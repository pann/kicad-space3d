#!/usr/bin/env bash
# Install kicad-space3d bridge: udev rule + systemd user unit + deps.
# Idempotent — safe to re-run.
#
# Run as your normal user. Will invoke sudo for the system bits.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${USER:-$(id -un)}"

step() { printf "\n=== %s\n" "$*"; }
note() { printf "    %s\n" "$*"; }

step "Checking apt packages (python3-evdev, xdotool)"
need_apt=()
dpkg -s python3-evdev >/dev/null 2>&1 || need_apt+=(python3-evdev)
dpkg -s xdotool       >/dev/null 2>&1 || need_apt+=(xdotool)
if [ ${#need_apt[@]} -gt 0 ]; then
    note "Installing: ${need_apt[*]}"
    sudo apt-get update
    sudo apt-get install -y "${need_apt[@]}"
else
    note "all present"
fi

step "Installing udev rule for /dev/uinput access"
sudo install -m 644 "$HERE/udev/70-kicad-space3d.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=misc

step "Ensuring uinput kernel module is loaded"
if ! lsmod | grep -q '^uinput'; then
    sudo modprobe uinput
fi
echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
note "uinput will autoload at boot"

step "Adding $USER_NAME to 'input' group (if missing)"
if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx input; then
    note "already in input group"
else
    sudo usermod -aG input "$USER_NAME"
    note "added — you must LOG OUT and back in for this to take effect"
fi

step "Fixing /dev/uinput permissions for current session"
sudo chgrp input /dev/uinput
sudo chmod 0660 /dev/uinput
note "(temporary; udev rule handles future boots)"

step "Installing systemd user unit"
mkdir -p "$HOME/.config/systemd/user"
install -m 644 "$HERE/systemd/kicad-space3d.service" \
    "$HOME/.config/systemd/user/kicad-space3d.service"
systemctl --user daemon-reload
systemctl --user enable kicad-space3d.service
note "enabled — will autostart on next login"

step "Done"
cat <<EOF

Next steps:
  1. If you were JUST added to the 'input' group, log out and back in.
  2. Start the bridge now:
       systemctl --user start kicad-space3d.service
  3. Check it's running:
       systemctl --user status kicad-space3d.service
       journalctl --user -fu kicad-space3d.service
  4. Open KiCad's PCB Editor and push the puck.

Tuning (override at runtime via systemd env file or 'systemctl --user edit'):
  KS3D_DEADZONE        default 600     (raise if puck drifts at rest)
  KS3D_PAN_SCALE       default 0.02    (raise for faster pan)
  KS3D_ZOOM_SCALE      default 0.001   (raise for faster zoom)
  KS3D_INVERT_Y        default 1       (set 0 if Y axis feels wrong)
  KS3D_TARGETS         default 'PCB Editor'
                       e.g. 'PCB Editor|Schematic Editor|3D Viewer'
EOF
