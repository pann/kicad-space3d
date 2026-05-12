"""
Reload the spacemouse_kicad plugin in a running KiCad without restart.

The plain `import spacemouse_kicad` form in the handoff loses references
to the previous _manager's running thread/timer — they keep going on
stale state. This script stops the old manager first, then reloads.

Usage from the PCB editor scripting console:

    exec(open('/home/pa/work/git/kicad-space3d/diag/reload_plugin.py').read())

After reload, the new manager is auto-started so you can immediately
try the puck. Re-run after every edit (arrow-up).
"""

import sys


def _reload():
    # Stop any previous instance and drop the modules.
    for modname in list(sys.modules.keys()):
        if "spacemouse_kicad" not in modname:
            continue
        mod = sys.modules[modname]
        mgr = getattr(mod, "_manager", None)
        if mgr is not None and getattr(mgr, "_running", False):
            print("[reload] stopping previous manager")
            try:
                mgr.stop()
            except Exception as e:
                print(f"[reload] previous stop raised: {type(e).__name__}: {e}")
        del sys.modules[modname]

    import spacemouse_kicad  # noqa: F401  (re-imports __init__.py, re-registers ActionPlugin)
    from spacemouse_kicad import spacemouse_action

    ok, reason = spacemouse_action._manager.start()
    if ok:
        print("[reload] manager started — puck should now move the view")
    else:
        print(f"[reload] manager start FAILED: {reason}")


_reload()
