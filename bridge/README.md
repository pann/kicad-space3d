# kicad-space3d bridge

Userspace daemon that injects synthetic mouse-wheel + middle-drag events
into the kernel input layer in response to 3Dconnexion SpaceMouse motion,
so KiCad sees them as if from a real mouse.

## Architecture

```
SpaceMouse  →  spacenavd  →  bridge.py  →  /dev/uinput  →  kernel input  →  KiCad
                                                                            (any version)
```

This replaces the in-process KiCad ActionPlugin approach, which corrupted
KiCad's heap when wxMouseEvents were synthesized into the wxGLCanvas at
rate. The bridge has zero coupling to KiCad's Python API or wx internals;
KiCad just sees a second virtual mouse called `kicad-space3d`.

Focus is checked via `xdotool getactivewindow` so the bridge only injects
when a target KiCad window (PCB Editor by default) is focused.

## Install

```bash
./install.sh
```

That runs an idempotent installer: installs `python3-evdev` and `xdotool`,
drops a udev rule for `/dev/uinput`, adds you to the `input` group,
ensures the `uinput` kernel module autoloads, and installs+enables a
systemd user unit.

If the installer added you to the `input` group, log out and back in
before starting the service.

## Use

```bash
systemctl --user start kicad-space3d.service
journalctl --user -fu kicad-space3d.service   # tail logs
systemctl --user stop kicad-space3d.service
```

Open KiCad's PCB Editor. Push the puck:

- **Tilt left/right, push fwd/back** → pan (middle-drag gesture)
- **Pull cap up / push down**        → zoom in / out (wheel gesture)
- **Twist**                          → unused for now

## Tuning

All knobs are env vars (override via `systemctl --user edit kicad-space3d.service`):

| var | default | meaning |
|---|---|---|
| `KS3D_DEADZONE` | 600 | raise if puck drifts at rest (raw unit, ±32000 max) |
| `KS3D_PAN_SCALE` | 0.02 | unit → pixels per event; raise to pan faster |
| `KS3D_ZOOM_SCALE` | 0.001 | unit → wheel notches per event |
| `KS3D_MAX_WHEEL_PER_EVENT` | 3 | cap on notches per event |
| `KS3D_INVERT_Y` | 1 | set 0 if Y axis pans the wrong way |
| `KS3D_FOCUS_CACHE_S` | 0.2 | xdotool poll interval |
| `KS3D_TARGETS` | `PCB Editor` | pipe-separated title substrings; e.g. `PCB Editor\|Schematic Editor` |
| `SPNAV_SOCK` | `/var/run/spnav.sock` | spacenavd socket path |
