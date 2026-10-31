#!/usr/bin/env bash
# Update imagegen from GitHub, safely:
#   1. fetch (changes nothing locally)
#   2. show the incoming PRs/commits/files, and flag sudo, systemd, .wslconfig, dependency and download changes
#   3. ask y/N, then fast-forward to exactly the commit that was shown
#   4. do only the follow-up steps the change needs
# Usage: ~/imagegen/update.sh
#
# The whole script is wrapped in main() so bash parses it fully before running:
# the fast-forward may rewrite this very file, and bash reads scripts lazily.
set -euo pipefail

main() {
  cd "$(dirname "$(readlink -f "$0")")"
  local VENV="$HOME/venvs/imagegen"

  if [ -t 1 ]; then
    B=$'\e[1m'; R=$'\e[1;31m'; Y=$'\e[1;33m'; G=$'\e[32m'; N=$'\e[0m'
  else
    B=''; R=''; Y=''; G=''; N=''
  fi

  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "!! Local edits found. Commit or stash them, then re-run:"
    git status --short --untracked-files=no
    exit 1
  fi

  local upstream
  upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) \
    || { echo "!! This branch has no upstream. Run: git branch -u origin/main"; exit 1; }

  echo "==> Checking $upstream for updates..."
  git fetch --quiet
  local old new
  old=$(git rev-parse HEAD)
  new=$(git rev-parse '@{u}')
  [ "$old" = "$new" ] && { echo "Already up to date."; exit 0; }
  if ! git merge-base --is-ancestor "$old" "$new"; then
    echo "!! Local commits not on $upstream. Not touching anything; sort it out with git first."
    exit 1
  fi

  local changed
  changed=$(git diff --name-only "$old" "$new")
  show_incoming "$old" "$new" "$changed"

  local answer=''
  # Ask the terminal directly; with no terminal (cron, pipe) the answer is "no".
  if { exec 3</dev/tty; } 2>/dev/null; then
    printf '%s' "${B}Apply these changes? [y/N] ${N}"
    read -r answer <&3 || answer=''
    exec 3<&-
  fi
  case "$answer" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "Nothing applied. (Fetched only; your files are unchanged.)"; exit 0 ;;
  esac

  # Apply exactly what was shown, even if the remote moved in the meantime.
  git merge --ff-only --quiet "$new"
  echo "${G}==> Updated to $(git log -1 --format='%h %s' "$new")${N}"

  has() { grep -qE "$1" <<<"$changed"; }

  if has '^requirements\.txt$'; then
    echo "==> requirements.txt changed: installing into $VENV"
    # Guard: torch & co never belong here (only ~/venvs/comfy has the ROCm builds).
    local reqs
    reqs=$(mktemp)
    grep -viE '^[[:space:]]*(torch|torchvision|torchaudio|triton)([^a-z0-9_-]|$)' requirements.txt > "$reqs" || true
    "$VENV/bin/pip" install -q -r "$reqs"
    rm -f "$reqs"
  fi

  if has '^comfy/'; then
    if [ "$(comfy_step "$new")" = install ]; then
      echo "==> ComfyUI service settings changed: reinstalling drop-in (asks for sudo, restarts comfyui)"
      ./comfy/install.sh
    else
      echo "==> ComfyUI files changed: restarting comfyui (asks for sudo)"
      sudo systemctl restart comfyui
    fi
  fi

  if has '^windows/'; then
    echo "==> Windows-side files changed. If the PR says so, run the script in windows/ by hand."
  fi

  if has '^setup\.sh$'; then
    echo "==> setup.sh changed. If the PR says so, run: ./setup.sh"
  fi

  if has '^(mcp_server|imagegen)\.py$'; then
    echo
    echo "############################################################"
    echo "#  MCP server code changed: quit and reopen the Claude app  #"
    echo "############################################################"
  fi

  echo "==> Done. The gen CLI already uses the new code."
}

# How a comfy/ change gets applied at commit $1: "install" if comfy/install.sh is committed executable.
comfy_step() {
  if git ls-tree "$1" -- comfy/install.sh | grep -q '^100755 '; then echo install; else echo restart; fi
}

# Print what old..new brings in, with risk flags and the follow-up steps update.sh will take.
show_incoming() {
  local old=$1 new=$2 changed=$3 sha subj body num title

  echo
  echo "${B}==> Incoming changes${N}"
  # First-parent history: one line per merged PR (or direct commit) on main.
  while IFS=$'\t' read -r sha subj; do
    if [[ $subj =~ ^Merge\ pull\ request\ \#([0-9]+) ]]; then
      num=${BASH_REMATCH[1]}
      title=$(git log -1 --format=%b "$sha" | sed '/^[[:space:]]*$/d' | head -1)
      echo "  PR #$num: ${title:-$subj}"
    elif [[ $subj =~ \(\#([0-9]+)\)$ ]]; then
      echo "  PR #${BASH_REMATCH[1]}: ${subj% (#*}"
    else
      echo "  $sha $subj"
    fi
  done < <(git log --first-parent --reverse --format=$'%h\t%s' "$old..$new")
  local remote_url
  remote_url=$(git remote get-url origin 2>/dev/null | sed -E 's#^git@github\.com:#https://github.com/#; s#\.git$##' || true)
  [[ $remote_url == https://github.com/* ]] && echo "  (details: $remote_url/pulls?q=is%3Apr+is%3Amerged)"

  echo
  echo "${B}==> Files${N}"
  git diff --stat=100 "$old" "$new" | sed 's/^/  /'

  # Risk scan: changed paths + added/removed lines ("file: +line" / "file: -line").
  local diff dlines flags=()
  diff=$(git diff -U0 --no-color "$old" "$new")
  dlines=$(awk '
    /^diff --git /{ f=$NF; sub(/^b\//, "", f); hdr=1; next }
    /^@@/         { hdr=0; next }
    hdr           { next }
    /^[+-]/       { print f ": " $0 }' <<<"$diff")

  flag() {  # flag "LABEL" "path regex" "changed-line regex"
    local label=$1 pre=$2 lre=$3 hits=''
    [ -n "$pre" ] && hits=$(grep -E "$pre" <<<"$changed" | sed 's/^/      file  /' || true)
    if [ -n "$lre" ]; then
      local lines
      lines=$(grep -E "$lre" <<<"$dlines" | grep -vE '^[^:]+: [+-][[:space:]]*#' | cut -c1-140 | sed 's/^/      line  /' || true)
      hits=$(printf '%s\n%s' "$hits" "$lines" | sed '/^$/d')
    fi
    [ -n "$hits" ] && flags+=("${R}!! $label${N}"$'\n'"$hits")
    return 0
  }
  flag "Uses sudo / root" \
       '' '(^|[^a-zA-Z_])(sudo|pkexec|doas)([^a-zA-Z_]|$)|/etc/'
  flag "systemd / services" \
       '(^comfy/|\.service$|\.timer$)' 'systemctl|systemd|\.service|\.timer|journalctl.*--vacuum'
  flag "WSL / Windows config (.wslconfig)" \
       '(^windows/|wslconfig)' '\.wslconfig|wsl(\.exe)? --(shutdown|terminate|unregister)|powershell\.exe|cmd\.exe|reg(\.exe)? add'
  flag "Dependencies" \
       '(^requirements[^/]*\.txt$|pyproject\.toml$|setup\.py$|package(-lock)?\.json$)' \
       'pip3? install|uv (pip|add)|apt(-get)? install|dpkg -i|snap install|npm (i|install)|amdgpu-install'
  flag "Downloads" \
       '' '(curl|wget|aria2c|git clone|hf_hub_download|huggingface-cli|urlretrieve|requests\.get|urlopen)|https?://[^ ]+\.(pth|safetensors|ckpt|onnx|bin|gguf|sh|deb|whl|zip|tar)'
  flag "Shell scripts that may be run" \
       '\.sh$' ''

  echo
  if [ ${#flags[@]} -eq 0 ]; then
    echo "${G}==> No sudo, systemd, .wslconfig, dependency or download changes found.${N}"
  else
    echo "${R}==> Needs a look before you say yes:${N}"
    printf '  %s\n' "${flags[@]}"
  fi

  echo
  echo "${B}==> If you say yes, update.sh will then:${N}"
  local any=0
  if grep -qE '^requirements\.txt$' <<<"$changed"; then
    any=1; echo "  ${Y}- pip install into ~/venvs/imagegen (torch/torchvision/torchaudio/triton filtered out):${N}"
    grep -E '^requirements\.txt: [+-]' <<<"$dlines" | sed 's/^requirements\.txt: /      /' || true
  fi
  if grep -qE '^comfy/' <<<"$changed"; then
    any=1
    if [ "$(comfy_step "$new")" = install ]; then
      echo "  ${Y}- run comfy/install.sh (sudo: systemd drop-in, daemon-reload, restart comfyui)${N}"
    else
      echo "  ${Y}- sudo systemctl restart comfyui${N}"
    fi
  fi
  if grep -qE '^update\.sh$' <<<"$changed"; then
    any=1; echo "  - (update.sh itself changes; the new version runs from next time)"
  fi
  [ $any -eq 0 ] && echo "  - nothing beyond updating the files"
  echo "  Scripts in windows/ and setup.sh are never run automatically."
  echo
}

main "$@"; exit
