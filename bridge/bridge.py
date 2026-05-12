#!/usr/bin/env python3
"""
kicad-space3d-bridge: spacenavd → /dev/uinput → KiCad

Reads 6DOF events from spacenavd's Unix socket and injects synthetic
mouse input via /dev/uinput, so KiCad sees them as if from a real mouse.
This bypasses KiCad's Python API entirely and is independent of KiCad
version / wx event-dispatcher quirks.

  TX, TY → middle-mouse-drag (KiCad's native pan gesture)
  TZ     → vertical wheel    (KiCad's native zoom gesture)

Only injects while the focused window is the KiCad PCB Editor (focus
checked via xdotool because wxWidgets runs through XWayland on
Ubuntu GNOME). Other windows are unaffected.

Requires:
  - /dev/uinput readable+writable by current user (see install.sh)
  - spacenavd running (systemctl status spacenavd)
  - python3-evdev (apt install python3-evdev)
  - xdotool (apt install xdotool)
"""

import os
import socket
import struct
import subprocess
import sys
import time

try:
    from evdev import UInput, ecodes as e
except ImportError:
    sys.stderr.write(
        "ERROR: python3-evdev is not installed.\n"
        "  sudo apt install python3-evdev\n"
    )
    sys.exit(1)


# === Tuning (env-overridable for quick iteration without editing) ============

def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


SPNAV_SOCK = os.environ.get("SPNAV_SOCK", "/var/run/spnav.sock")

# Substring(s) that identify a target window title. Pipe-separated.
# Default: PCB editor only. Extend with "PCB Editor|Schematic Editor" etc.
TARGET_WINDOW_SUBSTRINGS = os.environ.get(
    "KS3D_TARGETS", "PCB Editor"
).split("|")

# Drift filter — raw spacenavd unit, ~±32000 max.
DEADZONE = _env_int("KS3D_DEADZONE", 600)

# Pan: spacenavd unit → relative pixel delta per spacenavd event.
PAN_SCALE = _env_float("KS3D_PAN_SCALE", 0.02)

# Zoom: spacenavd unit → wheel notches per spacenavd event (clamped).
ZOOM_SCALE = _env_float("KS3D_ZOOM_SCALE", 0.001)
MAX_WHEEL_PER_EVENT = _env_int("KS3D_MAX_WHEEL_PER_EVENT", 3)

# Y axis sign: invert so pushing the puck *forward* pans the view *up*.
PAN_Y_INVERT = os.environ.get("KS3D_INVERT_Y", "1") == "1"

# Focus check cache lifetime.
FOCUS_CACHE_S = _env_float("KS3D_FOCUS_CACHE_S", 0.2)


# === Logging ================================================================

def log(msg):
    sys.stderr.write(f"[ks3d] {msg}\n")
    sys.stderr.flush()


# === Focus check ============================================================

class _Focus:
    last_t = 0.0
    last_focused = False


def is_target_focused():
    """xdotool getwindowname; cached for FOCUS_CACHE_S."""
    now = time.monotonic()
    if now - _Focus.last_t < FOCUS_CACHE_S:
        return _Focus.last_focused
    try:
        r = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, timeout=0.5,
        )
        title = (r.stdout or "").strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        title = ""
    _Focus.last_focused = any(s in title for s in TARGET_WINDOW_SUBSTRINGS)
    _Focus.last_t = now
    return _Focus.last_focused


# === uinput virtual device ==================================================

def make_uinput():
    caps = {
        e.EV_KEY: [e.BTN_MIDDLE],
        e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
    }
    try:
        return UInput(caps, name="kicad-space3d", vendor=0x1209, product=0xb3a5)
    except PermissionError:
        log("PermissionError opening /dev/uinput. Did you run install.sh and re-login?")
        sys.exit(2)
    except FileNotFoundError:
        log("/dev/uinput not found. Try: sudo modprobe uinput")
        sys.exit(2)


# === Injection state machine ================================================

class Bridge:
    def __init__(self, ui):
        self.ui = ui
        self.panning = False

    def start_pan(self):
        if not self.panning:
            self.ui.write(e.EV_KEY, e.BTN_MIDDLE, 1)
            self.ui.syn()
            self.panning = True

    def stop_pan(self):
        if self.panning:
            self.ui.write(e.EV_KEY, e.BTN_MIDDLE, 0)
            self.ui.syn()
            self.panning = False

    def pan(self, dx, dy):
        self.start_pan()
        if dx == 0 and dy == 0:
            return
        if dx:
            self.ui.write(e.EV_REL, e.REL_X, dx)
        if dy:
            self.ui.write(e.EV_REL, e.REL_Y, dy)
        self.ui.syn()

    def wheel(self, notches):
        if notches == 0:
            return
        self.ui.write(e.EV_REL, e.REL_WHEEL, notches)
        self.ui.syn()


# === spacenavd reader =======================================================

def connect_spnav():
    if not os.path.exists(SPNAV_SOCK):
        log(f"spacenavd socket missing at {SPNAV_SOCK}. systemctl status spacenavd")
        sys.exit(3)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SPNAV_SOCK)
    s.settimeout(1.0)
    log(f"connected to spacenavd at {SPNAV_SOCK}")
    return s


def main():
    log(f"targets={TARGET_WINDOW_SUBSTRINGS} deadzone={DEADZONE} "
        f"pan_scale={PAN_SCALE} zoom_scale={ZOOM_SCALE}")

    ui = make_uinput()
    log("opened /dev/uinput device 'kicad-space3d'")
    bridge = Bridge(ui)

    sock = connect_spnav()
    buf = b""

    try:
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    log("spacenavd socket closed; exiting")
                    break
                buf += chunk
            except socket.timeout:
                # Idle — drop any open pan so we don't hold middle-button forever.
                bridge.stop_pan()
                continue

            while len(buf) >= 32:
                packet, buf = buf[:32], buf[32:]
                vals = struct.unpack("<8i", packet)
                tx, ty, tz = vals[0], vals[1], vals[2]
                # buttons = vals[7]  # TODO: map buttons in v2

                if not is_target_focused():
                    bridge.stop_pan()
                    continue

                # Deadzone
                if abs(tx) < DEADZONE: tx = 0
                if abs(ty) < DEADZONE: ty = 0
                if abs(tz) < DEADZONE: tz = 0

                # Pan
                if tx or ty:
                    dx = int(tx * PAN_SCALE)
                    dy = int(-ty * PAN_SCALE) if PAN_Y_INVERT else int(ty * PAN_SCALE)
                    bridge.pan(dx, dy)
                else:
                    bridge.stop_pan()

                # Zoom (independent — KiCad wheel events are atomic, no drag state)
                if tz:
                    n = int(-tz * ZOOM_SCALE)
                    n = max(-MAX_WHEEL_PER_EVENT, min(MAX_WHEEL_PER_EVENT, n))
                    bridge.wheel(n)

    except KeyboardInterrupt:
        log("interrupted")
    finally:
        bridge.stop_pan()
        try:
            ui.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        log("clean shutdown")


if __name__ == "__main__":
    main()
