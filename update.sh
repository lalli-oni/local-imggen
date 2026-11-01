#!/usr/bin/env bash
# Update imagegen from GitHub, safely:
#   1. fetch (changes nothing locally)
#   2. show the incoming PRs/commits/files, and flag sudo, systemd, .wslconfig, dependency and download changes
#   3. ask y/N, then fast-forward to exactly the commit that was shown
#   4. do only the follow-up steps the change needs
# Usage: ~/imagegen/update.sh [--since REV]
#
# update.sh remembers the last commit whose follow-up steps it applied (.git/imagegen-applied), so
# changes you pulled by hand with `git pull` still get previewed and their steps applied.
# --since REV: treat REV as the last applied commit (re-run the steps for everything after it).
#
# The whole script is wrapped in main() so bash parses it fully before running:
# the fast-forward may rewrite this very file, and bash reads scripts lazily.
set -euo pipefail

main() {
  local since=''
  while [ $# -gt 0 ]; do
    case "$1" in
      --since) [ $# -ge 2 ] || { echo "!! --since needs a commit, e.g. --since HEAD~3"; exit 2; }
               since=$2; shift 2 ;;
      --since=*) since=${1#--since=}; [ -n "$since" ] || { echo "!! --since needs a commit"; exit 2; }; shift ;;
      -h|--help) sed -n '2,/^# The whole script/{/^# The whole script/d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
      *) echo "!! Unknown option: $1 (try --help)"; exit 2 ;;
    esac
  done

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
  local cur new old state
  cur=$(git rev-parse HEAD)
  new=$(git rev-parse '@{u}')
  state=$(git rev-parse --git-path imagegen-applied)
  if ! git merge-base --is-ancestor "$cur" "$new"; then
    echo "!! Local commits not on $upstream. Not touching anything; sort it out with git first."
    exit 1
  fi

  # old = the last commit whose follow-up steps ran. Usually HEAD, but older after a manual git pull.
  if [ -n "$since" ]; then
    old=$(git rev-parse -q --verify "$since^{commit}") \
      || { echo "!! --since: no such commit: $since"; exit 2; }
    git merge-base --is-ancestor "$old" "$cur" \
      || { echo "!! --since: $since is not in this branch's history."; exit 2; }
  elif [ -f "$state" ]; then
    old=$(git rev-parse -q --verify "$(head -c 64 "$state")^{commit}" 2>/dev/null) || old=''
    if [ -z "$old" ] || ! git merge-base --is-ancestor "$old" "$cur"; then
      echo "${Y}!! The last applied commit isn't behind your current files (reset or checkout?); starting from them.${N}"
      echo "   (To re-run the steps for older changes: update.sh --since REV)"
      old=$cur
    fi
  else
    old=$cur  # first run with this version: nothing to go on, so trust the current files
    echo "${Y}==> First run that keeps track of applied changes: taking your current files as applied.${N}"
    echo "   If you pulled by hand before this, re-run the steps with: update.sh --since REV (see git reflog)"
  fi

  if [ "$old" = "$new" ]; then
    echo "Already up to date."
    echo "$cur" > "$state" 2>/dev/null || true  # baseline for next time; not worth failing over
    exit 0
  fi

  local changed
  changed=$(git diff --name-only "$old" "$new")
  show_incoming "$old" "$new" "$changed" "$cur"
  if [ "$old" != "$cur" ]; then
    local pulled
    pulled=$(git rev-list --first-parent --count "$old..$cur")
    echo "${Y}==> Already in your files, but follow-up steps not run yet: $pulled of the changes above${N}"
    echo "${Y}    (pulled by hand, or picked with --since)${N}"
    echo
  fi

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
  if [ "$cur" = "$new" ]; then
    echo "${G}==> Files already at $(git log -1 --format='%h %s' "$new"); running the follow-up steps${N}"
  else
    git merge --ff-only --quiet "$new"
    echo "${G}==> Updated to $(git log -1 --format='%h %s' "$new")${N}"
  fi

  has() { grep -qE "$1" <<<"$changed"; }

  if has '^requirements\.txt$'; then
    echo "==> requirements.txt changed: installing into $VENV"
    # Guard: torch & co never belong here (only ~/venvs/comfy has the ROCm builds).
    local reqs
    reqs=$(mktemp)
    # shellcheck disable=SC2064  # expand now: $reqs is a local, gone by the time EXIT fires
    trap "rm -f '$reqs'" EXIT  # also if pip fails (mktemp paths have no quotes)
    grep -viE '^[[:space:]]*(torch|torchvision|torchaudio|triton)([^a-z0-9_-]|$)' requirements.txt > "$reqs" || true
    "$VENV/bin/pip" install -q -r "$reqs"
    rm -f "$reqs"
    trap - EXIT
  fi

  if has '^comfy/'; then
    if [ "$(comfy_step "$new")" = install ]; then
      echo "==> ComfyUI settings changed: running comfy/install.sh"
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

  # Only now: if a step above failed (set -e), the next run offers the same steps again.
  echo "$new" > "$state"
  echo "==> Done. The gen CLI already uses the new code."
}

# How a comfy/ change gets applied at commit $1: "install" if comfy/install.sh is committed executable.
comfy_step() {
  if git ls-tree "$1" -- comfy/install.sh | grep -q '^100755 '; then echo install; else echo restart; fi
}

# Print what old..new brings in, with risk flags and the follow-up steps update.sh will take.
show_incoming() {
  local old=$1 new=$2 changed=$3 cur=$4 sha subj num title

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
      echo "  ${Y}- run comfy/install.sh: re-applies resource limits only if they are already on${N}"
      echo "  ${Y}  (then: sudo, systemd drop-in, restart comfyui); if limits are off it does nothing${N}"
    else
      echo "  ${Y}- sudo systemctl restart comfyui${N}"
    fi
  fi
  if git diff --quiet "$cur" "$new" -- update.sh; then :; else
    any=1; echo "  - (update.sh itself changes; the new version runs from next time)"
  fi
  [ $any -eq 0 ] && echo "  - nothing beyond updating the files"
  echo "  Scripts in windows/, models/ and setup.sh are never run automatically."
  echo
}

main "$@"; exit
