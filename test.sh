#!/usr/bin/env bash
# Run the test suite. Needs no GPU, ComfyUI, WSL or Windows: ComfyUI, systemctl, sudo, wslpath,
# powershell.exe and rembg are faked (see tests/conftest.py), and outputs go to a temp folder.
#
#   ./test.sh                  all tests (browser tests skip without playwright + Chromium)
#   ./test.sh --browser        also install playwright and run the contact sheet in Chromium
#   ./test.sh -k projects -x   anything else goes to pytest
#
# First run creates .venv-test/ here from requirements-dev.txt (re-installed when that file changes).
# It never touches ~/venvs/comfy or ~/venvs/imagegen.
# IMAGEGEN_TEST_PYTHON=python3 uses that interpreter as-is instead (CI does, after its own pip install).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

browser=0
args=()
for a in "$@"; do
  case "$a" in --browser) browser=1 ;; *) args+=("$a") ;; esac
done

# Chromium that ships with cloud sessions; Playwright's own download is used otherwise.
if [ -z "${IMAGEGEN_TEST_CHROMIUM:-}" ] && [ -x /opt/pw-browsers/chromium ]; then
  export IMAGEGEN_TEST_CHROMIUM=/opt/pw-browsers/chromium
  browser=1
fi

if [ -n "${IMAGEGEN_TEST_PYTHON:-}" ]; then
  py=$IMAGEGEN_TEST_PYTHON
else
  venv=.venv-test
  py=$venv/bin/python
  reqs=(requirements-dev.txt)
  [ "$browser" = 1 ] && reqs+=(requirements-browser.txt)
  stamp=$(cat "${reqs[@]}" | sha1sum | cut -c1-12)
  if [ ! -x "$py" ] || [ "$(cat "$venv/.stamp" 2>/dev/null)" != "$stamp" ]; then
    echo "==> setting up $venv (${reqs[*]})"
    [ -x "$py" ] || python3 -m venv "$venv"
    # torch & co never belong in a test venv either
    for r in "${reqs[@]}"; do
      if grep -qiE '^[[:space:]]*(torch|torchvision|torchaudio|triton)([^a-z0-9_-]|$)' "$r"; then
        echo "!! $r lists torch/torchvision/torchaudio/triton; remove it" >&2; exit 1
      fi
    done
    "$venv/bin/pip" install -q --upgrade pip
    "$venv/bin/pip" install -q "${reqs[@]/#/-r}"
    echo "$stamp" > "$venv/.stamp"
  fi
  export PATH="$PWD/$venv/bin:$PATH"  # puts the shellcheck binary from shellcheck-py on PATH
fi

if [ "$browser" = 1 ] && [ -z "${IMAGEGEN_TEST_CHROMIUM:-}" ]; then
  "$py" -m playwright install chromium >/dev/null || echo "!! playwright install chromium failed; browser tests will skip" >&2
fi

exec "$py" -m pytest "${args[@]+"${args[@]}"}"
