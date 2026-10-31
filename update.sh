#!/usr/bin/env bash
# Pull the latest imagegen from GitHub, then do only what the change needs.
# Usage: ~/imagegen/update.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
VENV="$HOME/venvs/imagegen"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "!! Local edits found. Commit or stash them, then re-run:"
  git status --short --untracked-files=no
  exit 1
fi

old=$(git rev-parse HEAD)
git pull --ff-only --quiet
new=$(git rev-parse HEAD)
[ "$old" = "$new" ] && { echo "Already up to date."; exit 0; }

echo "==> New commits:"
git log --oneline "$old..$new"
changed=$(git diff --name-only "$old" "$new")
has() { grep -qE "$1" <<<"$changed"; }

if has '^requirements\.txt$'; then
  echo "==> requirements.txt changed: installing into $VENV"
  # Guard: torch & co never belong here (only ~/venvs/comfy has the ROCm builds).
  reqs=$(mktemp)
  grep -viE '^[[:space:]]*(torch|torchvision|torchaudio|triton)([^a-z0-9_-]|$)' requirements.txt > "$reqs" || true
  "$VENV/bin/pip" install -q -r "$reqs"
  rm -f "$reqs"
fi

if has '^comfy/'; then
  if [ -f /etc/systemd/system/comfyui.service.d/zz-imagegen-limits.conf ]; then
    echo "==> Resource-limit settings changed and limits are ON: re-applying (sudo, restarts comfyui)"
    ./comfy/limits.sh on
  else
    echo "==> Resource-limit settings changed. Limits are off, so nothing was installed."
    echo "    Turn on when the PC is in use: ~/imagegen/comfy/limits.sh on"
  fi
fi

if has '^windows/'; then
  echo "==> Windows-side files changed. If the commit says so, run: ./windows/install-wslconfig.sh"
fi

if has '^setup\.sh$'; then
  echo "==> setup.sh changed. If the commit says so, run: ./setup.sh"
fi

if has '^(mcp_server|imagegen)\.py$'; then
  echo
  echo "############################################################"
  echo "#  MCP server code changed: quit and reopen the Claude app  #"
  echo "############################################################"
fi

echo "==> Done. The gen CLI already uses the new code."
