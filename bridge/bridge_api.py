#!/usr/bin/env python3
"""
kicad-space3d IPC bridge: spacenavd → KiCad IPC PanView / ZoomView

Pan and zoom go directly through the KiCad API — no uinput, no modifier keys,
no synthetic wheel events.  Normal mouse scroll is completely unaffected.

Requirements:
  - KiCad built with IPC pan/zoom support (PanView / ZoomView commands)
  - kicad-python (kipy) installed in this venv
  - spacenavd running
  - "Enable KiCad API" turned on in KiCad preferences
"""

import argparse
import os
import socket
import struct
import subprocess
import sys
import time

try:
    from kipy import KiCad
    from kipy.errors import ApiError
    from kipy.proto.common.types import DocumentType
except ImportError:
    sys.stderr.write("ERROR: kicad-python missing. Install in bridge/.venv:\n"
                     "  bridge/.venv/bin/pip install -e ~/work/git/kicad-python\n")
    sys.exit(1)


# === Tuning =================================================================

def _f(name, default):
    try: return float(os.environ.get(name, default))
    except ValueError: return default

def _i(name, default):
    try: return int(os.environ.get(name, default))
    except ValueError: return default

SPNAV_SOCK = os.environ.get("SPNAV_SOCK", "/var/run/spnav.sock")

PAN_DEADZONE    = _i("KS3D_PAN_DEADZONE", 5)
PAN_XY_DEADZONE = _i("KS3D_PAN_XY_DEADZONE", 20)  # combined magnitude gate
ZOOM_DEADZONE   = _i("KS3D_ZOOM_DEADZONE", 25)

# spacenavd unit → viewport fraction per event (0.0002 ≈ 0.02% of visible area)
PAN_SCALE  = _f("KS3D_PAN_SCALE", 0.0002)
# factor = 1.0 + tz * ZOOM_SCALE  (0.00003 × 100 units ≈ 0.3% zoom per event)
ZOOM_SCALE = _f("KS3D_ZOOM_SCALE", 0.00003)

def _parse_args():
    p = argparse.ArgumentParser(description="kicad-space3d IPC bridge")
    p.add_argument("--invert-x", action=argparse.BooleanOptionalAction,
                   default=os.environ.get("KS3D_PAN_X_INVERT", "0") == "1",
                   help="invert X pan direction (default: on)")
    p.add_argument("--invert-y", action=argparse.BooleanOptionalAction,
                   default=os.environ.get("KS3D_PAN_Y_INVERT", "1") == "1",
                   help="invert Y pan direction (default: on)")
    return p.parse_args()

_ARGS = _parse_args()
PAN_X_INVERT  = _ARGS.invert_x
PAN_Y_INVERT  = _ARGS.invert_y

TARGET_WINDOW_SUBSTRINGS = os.environ.get(
    "KS3D_TARGETS", "PCB Editor"
).split("|")
FOCUS_CACHE_S = _f("KS3D_FOCUS_CACHE_S", 0.3)

DEBUG = os.environ.get("KS3D_DEBUG", "0") == "1"


# === Logging ================================================================

def log(msg):
    sys.stderr.write(f"[ks3d-ipc] {msg}\n")
    sys.stderr.flush()


# === Focus check ============================================================

class _Focus:
    last_t = 0.0
    last_focused = False

def is_target_focused():
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
    if DEBUG:
        log(f"focus: title={title!r} -> {_Focus.last_focused}")
    return _Focus.last_focused


# === Document resolver ======================================================

def get_pcb_document(kc):
    """Return the first open PCB document, or None if no board is open."""
    try:
        docs = kc.get_open_documents(DocumentType.DOCTYPE_PCB)
        return docs[0] if docs else None
    except ApiError:
        return None


# === spacenavd reader =======================================================

def connect_spnav():
    if not os.path.exists(SPNAV_SOCK):
        log(f"spacenavd socket missing at {SPNAV_SOCK}.")
        sys.exit(3)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SPNAV_SOCK)
    s.settimeout(0.5)
    log(f"connected to spacenavd at {SPNAV_SOCK}")
    return s


# === Main ===================================================================

def main():
    log(f"targets={TARGET_WINDOW_SUBSTRINGS} "
        f"pan_dz={PAN_DEADZONE} pan_xy_dz={PAN_XY_DEADZONE} zoom_dz={ZOOM_DEADZONE} "
        f"pan_scale={PAN_SCALE} zoom_scale={ZOOM_SCALE} "
        f"invert_x={PAN_X_INVERT} invert_y={PAN_Y_INVERT}")

    kc = None
    doc = None

    def reconnect():
        nonlocal kc, doc
        try:
            kc = KiCad()
            log(f"connected to KiCad API (KiCad {kc.get_version()}, "
                f"API {kc.get_api_version()})")
            doc = get_pcb_document(kc)
            if doc is not None:
                log(f"PCB document: {doc.board_filename}")
            else:
                log("WARNING: no PCB document open; will retry per event")
        except Exception as err:
            log(f"KiCad connection failed: {err}; will retry")
            kc = None
            doc = None

    reconnect()

    sock = connect_spnav()
    buf = b""

    try:
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    log("spacenavd closed; exiting")
                    break
                buf += chunk
            except socket.timeout:
                continue

            # Discard all but the latest complete packet to avoid replaying
            # stale events that built up while KiCad was busy.
            if len(buf) >= 32:
                n = (len(buf) // 32) * 32
                buf = buf[n - 32:]

            while len(buf) >= 32:
                packet, buf = buf[:32], buf[32:]
                vals = struct.unpack("<8i", packet)
                tx, ty, tz = vals[1], vals[3], vals[2]

                if DEBUG and any(v != 0 for v in vals):
                    log(f"raw tx={tx} ty={ty} tz={tz}")

                if not is_target_focused():
                    continue

                if kc is None:
                    reconnect()
                if kc is None:
                    continue

                if doc is None:
                    doc = get_pcb_document(kc)
                if doc is None:
                    continue

                if abs(tx) < PAN_DEADZONE:  tx = 0
                if abs(ty) < PAN_DEADZONE:  ty = 0
                if tx*tx + ty*ty < PAN_XY_DEADZONE*PAN_XY_DEADZONE: tx = ty = 0
                if abs(tz) < ZOOM_DEADZONE: tz = 0

                if tx or ty:
                    dx = tx * PAN_SCALE * (-1 if PAN_X_INVERT else 1)
                    dy = ty * PAN_SCALE * (-1 if PAN_Y_INVERT else 1)
                    if DEBUG:
                        log(f"pan_view dx={dx:.5f} dy={dy:.5f}")
                    try:
                        kc.pan_view(doc, dx, dy)
                    except ApiError as err:
                        if DEBUG:
                            log(f"pan_view failed: {err}")
                        kc = doc = None

                if tz and kc is not None:
                    factor = 1.0 + (-tz) * ZOOM_SCALE
                    try:
                        kc.zoom_view(doc, factor)
                    except ApiError as err:
                        if DEBUG:
                            log(f"zoom_view failed: {err}")
                        kc = doc = None

    except KeyboardInterrupt:
        log("interrupted")
    finally:
        try: sock.close()
        except Exception: pass
        log("clean shutdown")


if __name__ == "__main__":
    main()
