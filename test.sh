#!/usr/bin/env bash
# Run the test suite. Needs no GPU, ComfyUI, WSL or Windows: ComfyUI, systemctl, sudo, wslpath,
# powershell.exe and rembg are faked (see tests/conftest.py), and outputs go to a temp folder.
#
#   ./test.sh                  all tests (browser tests skip without playwright + Chromium)
#   ./test.sh --browser        also install playwright and run the contact sheet in Chromium
#   ./test.sh -k projects -x   anything else goes to pytest
#
# First run creates .venv-test/ here from requirements-dev.txt (each file re-installed when it changes).
# IMAGEGEN_TEST_CHROMIUM=/path/to/chrome uses that browser (and implies --browser).
# It never touches ~/venvs/comfy or ~/venvs/imagegen.
# IMAGEGEN_TEST_PYTHON=python3 uses that interpreter as-is instead (CI does, after its own pip install).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

browser=0
args=()
for a in "$@"; do
  case "$a" in --browser) browser=1 ;; *) args+=("$a") ;; esac
done

# A Chromium you point at turns the browser tests on; so does the one that ships with cloud sessions.
if [ -z "${IMAGEGEN_TEST_CHROMIUM:-}" ] && [ -x /opt/pw-browsers/chromium ]; then
  export IMAGEGEN_TEST_CHROMIUM=/opt/pw-browsers/chromium
fi
[ -n "${IMAGEGEN_TEST_CHROMIUM:-}" ] && browser=1

if [ -n "${IMAGEGEN_TEST_PYTHON:-}" ]; then
  py=$IMAGEGEN_TEST_PYTHON
else
  venv=.venv-test
  py=$venv/bin/python
  reqs=(requirements-dev.txt)
  [ "$browser" = 1 ] && reqs+=(requirements-browser.txt)
  # torch & co never belong in a test venv either
  for r in "${reqs[@]}"; do
    if grep -qiE '^[[:space:]]*(torch|torchvision|torchaudio|triton)([^a-z0-9_-]|$)' "$r"; then
      echo "!! $r lists torch/torchvision/torchaudio/triton; remove it" >&2; exit 1
    fi
  done
  if [ ! -x "$py" ]; then
    echo "==> creating $venv"
    python3 -m venv "$venv"
    "$venv/bin/pip" install -q --upgrade pip
  fi
  # one stamp per requirements file: (re)install a file only when it changed since its last install
  for r in "${reqs[@]}"; do
    stamp="$venv/.stamp-$r"
    if [ "$(cat "$stamp" 2>/dev/null)" != "$(sha1sum < "$r")" ]; then
      echo "==> installing $r into $venv"
      "$venv/bin/pip" install -q -r "$r"
      sha1sum < "$r" > "$stamp"
    fi
  done
  export PATH="$PWD/$venv/bin:$PATH"  # puts the shellcheck binary from shellcheck-py on PATH
fi

if [ "$browser" = 1 ] && [ -z "${IMAGEGEN_TEST_CHROMIUM:-}" ]; then
  # a no-op once Playwright's Chromium is downloaded
  "$py" -m playwright install chromium >/dev/null || echo "!! playwright install chromium failed; browser tests will skip" >&2
fi

exec "$py" -m pytest "${args[@]+"${args[@]}"}"
