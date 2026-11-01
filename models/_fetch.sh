# Shared by the opt-in model download scripts in this folder (sourced, not run).
# The caller sets, then sources this file:
#   TITLE    what the models are for ("FLUX.1 Kontext models")
#   FILES    "folder|file|path on huggingface.co|approx size in GB" entries
#   LICENCE  one line printed before asking (licence + model page)
#   TRY      what to run once everything is there
# Args: -y/--yes (no prompt), -h/--help (prints the caller's header comment).
# Safe to re-run: skips files that are there, resumes partial downloads (*.part), checks sha256
# when Hugging Face sends it. Only writes model files: no sudo, no pip, never touches ~/venvs/comfy
# or the comfyui service (ComfyUI sees new model files without a restart).
# shellcheck shell=bash disable=SC2154

MODELS="${COMFY_MODELS:-$HOME/comfyui/models}"
HF="${HF_URL:-https://huggingface.co}"

ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,/^set -/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: $0 [-y]" >&2; exit 2 ;;
  esac
done

command -v wget >/dev/null || { echo "wget not found (sudo apt install wget)" >&2; exit 1; }
[ -d "$MODELS" ] || { echo "$MODELS not found (is ComfyUI installed in ~/comfyui?)" >&2; exit 1; }

auth=()
[ -n "${HF_TOKEN:-}" ] && auth=(--header "Authorization: Bearer $HF_TOKEN")

missing=()
need=0
echo "==> $TITLE in $MODELS"
for f in "${FILES[@]}"; do
  IFS='|' read -r folder name _ size <<<"$f"
  if [ -f "$MODELS/$folder/$name" ]; then
    echo "  ok       $folder/$name"
  else
    echo "  missing  $folder/$name (${size} GB)"
    missing+=("$f")
    need=$(awk -v a="$need" -v b="$size" 'BEGIN { print a + b }')
  fi
done
if [ ${#missing[@]} -eq 0 ]; then
  echo "All there. Try: $TRY"
  exit 0
fi

avail_gb=$(df -BG --output=avail "$MODELS" | tail -1 | tr -dc '0-9')
echo
echo "To download: about ${need} GB. Free on that disk: ${avail_gb} GB."
if awk -v a="$avail_gb" -v n="$need" 'BEGIN { exit !(a < n + 1) }'; then
  echo "!! Probably not enough space (a resumed *.part needs less)."
fi
echo "$LICENCE"

if [ $ASSUME_YES -eq 0 ]; then
  # No terminal means no.
  if { exec 3</dev/tty; } 2>/dev/null; then
    printf 'Download the missing files now? [y/N] '
    read -r answer <&3 || answer=''
  else
    answer=''
  fi
  [[ $answer =~ ^[Yy] ]] || { echo "Nothing downloaded."; exit 1; }
fi

# HF serves the sha256 of LFS files as X-Linked-Etag; verify when we can get it.
expected_sha() {
  command -v curl >/dev/null || return 0
  curl -sSI ${HF_TOKEN:+-H "Authorization: Bearer $HF_TOKEN"} "$1" 2>/dev/null \
    | tr -d '\r' | awk -F': ' 'tolower($1)=="x-linked-etag" {gsub(/"/, "", $2); print $2; exit}'
}

for f in "${missing[@]}"; do
  IFS='|' read -r folder name path size <<<"$f"
  url="$HF/$path"
  dest="$MODELS/$folder/$name"
  mkdir -p "$MODELS/$folder"
  echo
  echo "==> $folder/$name (${size} GB)"
  wget -c -q --show-progress "${auth[@]}" -O "$dest.part" "$url" || {
    echo "!! download failed. Re-run to resume. If it says 401/403, the file may need a Hugging Face"
    echo "   login: accept the licence on the model page, then HF_TOKEN=hf_... $0" >&2
    exit 1
  }
  sha=$(expected_sha "$url" || true)
  if [[ $sha =~ ^[0-9a-f]{64}$ ]]; then
    echo "    verifying sha256..."
    got=$(sha256sum "$dest.part" | cut -d' ' -f1)
    if [ "$got" != "$sha" ]; then
      rm -f "$dest.part"
      echo "!! checksum mismatch for $name (deleted the bad file; re-run to download it again)" >&2
      exit 1
    fi
  else
    echo "    (no checksum from Hugging Face, skipped verification)"
  fi
  mv "$dest.part" "$dest"
done

echo
echo "Done. Check with: gen doctor"
echo "Then try: $TRY"
