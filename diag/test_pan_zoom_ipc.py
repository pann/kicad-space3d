#!/usr/bin/env python3
"""
Verify PanView and ZoomView IPC commands work against a running KiCad
built with IPC pan/zoom support.

Usage:
    bridge/.venv/bin/python diag/test_pan_zoom_ipc.py

Requires: KiCad from feature/ipc-pan-zoom-viewport running with a board open.
"""

import sys
import time

try:
    from kipy import KiCad
    from kipy.errors import ApiError
    from kipy.proto.common.types import DocumentType
except ImportError:
    sys.exit("ERROR: kicad-python not installed in this venv")


def main():
    kc = KiCad()
    print(f"Connected: KiCad {kc.get_version()}, API {kc.get_api_version()}")

    docs = kc.get_open_documents(DocumentType.DOCTYPE_PCB)
    if not docs:
        sys.exit("ERROR: no PCB document open — open a board in the PCB editor first")

    doc = docs[0]
    print(f"Board: {doc.board_filename}")

    print("\n--- PanView test ---")
    print("Panning right 10% of viewport...")
    kc.pan_view(doc, 0.1, 0.0)
    time.sleep(0.5)
    print("Panning back left 10%...")
    kc.pan_view(doc, -0.1, 0.0)
    time.sleep(0.5)
    print("Panning down 10%...")
    kc.pan_view(doc, 0.0, 0.1)
    time.sleep(0.5)
    print("Panning back up 10%...")
    kc.pan_view(doc, 0.0, -0.1)
    time.sleep(0.5)
    print("PanView: OK")

    print("\n--- ZoomView test ---")
    print("Zooming in 10%...")
    kc.zoom_view(doc, 1.1)
    time.sleep(0.5)
    print("Zooming back out 10%...")
    kc.zoom_view(doc, 1.0 / 1.1)
    time.sleep(0.5)
    print("ZoomView: OK")

    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
