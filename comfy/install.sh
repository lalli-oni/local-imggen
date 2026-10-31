#!/usr/bin/env bash
# Called by update.sh whenever comfy/ changes. Must stay committed executable (update.sh checks the bit).
# Resource limits are opt-in: this only re-applies them if they are already on, and otherwise
# does nothing (no sudo, no restart). Turn them on/off yourself with comfy/limits.sh.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
if [ -f /etc/systemd/system/comfyui.service.d/zz-imagegen-limits.conf ]; then
  echo "==> Resource limits are ON: re-applying with the new settings (sudo, restarts comfyui)"
  exec "$HERE/limits.sh" on
fi
echo "==> Resource limits are off, so nothing was installed."
echo "    Turn them on when the PC is in use: ~/imagegen/comfy/limits.sh on"
