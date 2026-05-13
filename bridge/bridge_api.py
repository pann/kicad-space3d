#!/usr/bin/env python3
"""
kicad-space3d button bridge: spacenavd → xdotool key injection

KiCad's native SpaceMouse support handles pan/zoom/rotate. This bridge maps
SpaceMouse buttons to keyboard shortcuts via xdotool, with separate maps for
the PCB Editor and 3D Viewer.

Button numbers are 1-based (1–15 for a 15-button device). Button 0 is the
release sentinel and cannot be mapped.

Modifier keys (shift, ctrl, alt) are held via keydown on press and released
via keyup when the physical button is released (vals[1]==0). They remain held
in the X server, so mouse clicks and other keys see the modifier active.

Configuration (env vars):
  KS3D_PCB_BUTTONS   Comma-separated button:key for PCB Editor
  KS3D_3D_BUTTONS    Comma-separated button:key for 3D Viewer
  SPNAV_SOCK         Path to spacenavd socket (default: /var/run/spnav.sock)
  KS3D_FOCUS_CACHE_S Focus check cache in seconds (default: 0.3)
  KS3D_DEBUG         Set to 1 for verbose logging
"""

import os
import socket
import struct
import subprocess
import sys
import time

SPNAV_SOCK     = os.environ.get("SPNAV_SOCK", "/var/run/spnav.sock")
DEBUG          = os.environ.get("KS3D_DEBUG", "0") == "1"
FOCUS_CACHE_S  = float(os.environ.get("KS3D_FOCUS_CACHE_S", "0.3"))

# Keys that should be held (keydown on press, keyup on release)
HOLD_KEYS = {"shift", "ctrl", "alt", "super", "meta"}

# Default maps — override with KS3D_PCB_BUTTONS / KS3D_3D_BUTTONS env vars.
# NOTE: button 0 (release sentinel) cannot be mapped.
_DEFAULT_PCB = (
    "6:Escape,"
    "8:shift,"
    "9:ctrl,"
    "7:alt,"
    "1:Prior,"        # PgUp  — Top Cu active
    # button 0 (PgDn / Bottom Cu) is the release sentinel — not mappable
    "13:ctrl+plus,"   # up one layer
    "12:ctrl+minus,"  # down one layer
    "10:Home,"        # zoom fit board
    "11:f"            # zoom selection
)
_DEFAULT_3D = (
    "2:z,"            # top view
    "3:shift+x,"      # left view
    "4:x,"            # right view
    "5:shift+z,"      # bottom view
    "14:ctrl+r,"      # toggle ray-tracing
    "10:Home"         # fit view
)


def _parse_map(env_var, default):
    raw = os.environ.get(env_var, default)
    mapping = {}
    for item in raw.split(","):
        item = item.strip()
        if ":" not in item:
            continue
        btn, key = item.split(":", 1)
        try:
            mapping[int(btn.strip())] = key.strip()
        except ValueError:
            pass
    return mapping


PCB_MAP    = _parse_map("KS3D_PCB_BUTTONS", _DEFAULT_PCB)
VIEWER_MAP = _parse_map("KS3D_3D_BUTTONS", _DEFAULT_3D)


def log(msg):
    sys.stderr.write(f"[ks3d] {msg}\n")
    sys.stderr.flush()


# === Focus / context detection ==============================================

class _FocusCache:
    last_t = 0.0
    last_context = None  # "pcb", "3d", or None
    last_wid = None


def get_context():
    """Return (context, window_id) for the active window, cached."""
    now = time.monotonic()
    if now - _FocusCache.last_t < FOCUS_CACHE_S:
        return _FocusCache.last_context, _FocusCache.last_wid

    try:
        r = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=0.5,
        )
        wid = int(r.stdout.strip())
        r2 = subprocess.run(
            ["xdotool", "getwindowname", str(wid)],
            capture_output=True, text=True, timeout=0.5,
        )
        title = r2.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError, ValueError):
        wid, title = None, ""

    if "3D Viewer" in title:
        ctx = "3d"
    elif "PCB Editor" in title:
        ctx = "pcb"
    else:
        ctx = None

    _FocusCache.last_context = ctx
    _FocusCache.last_wid = wid
    _FocusCache.last_t = now
    if DEBUG:
        log(f"focus: {title!r} wid={wid} -> {ctx}")
    return ctx, wid


# === Key injection ==========================================================

def xdo(*args):
    try:
        subprocess.run(["xdotool"] + list(args), timeout=0.5)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as err:
        if DEBUG:
            log(f"xdotool {args} failed: {err}")


# === spacenavd reader =======================================================

def connect_spnav():
    if not os.path.exists(SPNAV_SOCK):
        log(f"spacenavd socket missing at {SPNAV_SOCK}")
        sys.exit(3)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SPNAV_SOCK)
    s.settimeout(0.5)
    log(f"connected to spacenavd at {SPNAV_SOCK}")
    return s


# === Main ===================================================================

def main():
    log(f"pcb={PCB_MAP}  3d={VIEWER_MAP}")

    sock = connect_spnav()
    buf = b""
    held_modifiers = {}  # key -> wid it was sent to

    try:
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    log("spacenavd closed; exiting")
                    break
                buf += chunk
            except socket.timeout:
                pass

            while len(buf) >= 32:
                pkt, buf = buf[:32], buf[32:]
                # All spnav events are 32 bytes (sizeof union spnav_event).
                # Motion (type=1): TX TY TZ RX RY RZ period — 7 ints of payload.
                # Button (type=2): bnum — 1 int of payload, rest zero padding.
                # Reading 8 bytes at a time split motion packets and caused TY/TZ
                # values to be misread as button events.
                event_type, button = struct.unpack_from("<2i", pkt)

                if event_type != 2:
                    continue

                if button:  # press — button number 1-15
                    ctx, wid = get_context()
                    bmap = PCB_MAP if ctx == "pcb" else VIEWER_MAP if ctx == "3d" else {}
                    key = bmap.get(button)
                    if key is None:
                        if DEBUG:
                            log(f"button {button} (unmapped, ctx={ctx})")
                        continue

                    w = ["--window", str(wid)] if wid else []
                    if key.lower() in HOLD_KEYS:
                        xdo("keydown", *w, key)
                        held_modifiers[key] = wid
                        if DEBUG:
                            log(f"button {button} keydown {key} wid={wid}")
                    else:
                        xdo("key", *w, key)
                        if DEBUG:
                            log(f"button {button} key {key} wid={wid}")

                else:  # release (vals[1] == 0)
                    for key, wid in list(held_modifiers.items()):
                        w = ["--window", str(wid)] if wid else []
                        xdo("keyup", *w, key)
                        if DEBUG:
                            log(f"keyup {key} wid={wid}")
                    held_modifiers.clear()

    except KeyboardInterrupt:
        log("interrupted")
    finally:
        for key, wid in list(held_modifiers.items()):
            w = ["--window", str(wid)] if wid else []
            xdo("keyup", *w, key)
        try:
            sock.close()
        except Exception:
            pass
        log("clean shutdown")


if __name__ == "__main__":
    main()
