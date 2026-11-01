#!/usr/bin/env bash
# Opt-in download of the models behind `gen edit` (FLUX.1 Kontext [dev], fp8) into ~/comfyui/models.
#   ~/imagegen/models/flux-kontext.sh        # shows what is missing, asks y/N, downloads
#   ~/imagegen/models/flux-kontext.sh -y     # no prompt
# Safe to re-run: skips files that are there, resumes partial downloads (*.part).
# Only writes model files. No sudo, no pip, never touches ~/venvs/comfy or the comfyui service
# (ComfyUI sees new model files without a restart).
# Licence: FLUX.1 [dev] Non-Commercial License (personal/non-commercial use).
set -euo pipefail

MODELS="${COMFY_MODELS:-$HOME/comfyui/models}"
HF="https://huggingface.co"
# folder | file | path on huggingface.co | approx size in GB
FILES=(
  "diffusion_models|flux1-dev-kontext_fp8_scaled.safetensors|Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors|11.9"
  "text_encoders|t5xxl_fp8_e4m3fn_scaled.safetensors|comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors|5.2"
  "text_encoders|clip_l.safetensors|comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors|0.25"
  "vae|ae.safetensors|Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors|0.33"
)

ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: $0 [-y]" >&2; exit 2 ;;
  esac
done

command -v wget >/dev/null || { echo "wget not found (sudo apt install wget)" >&2; exit 1; }
[ -d "$MODELS" ] || { echo "$MODELS not found (is ComfyUI installed in ~/comfyui?)" >&2; exit 1; }

auth=()
[ -n "${HF_TOKEN:-}" ] && auth=(--header "Authorization: Bearer $HF_TOKEN")

missing=()
need=0
echo "==> FLUX.1 Kontext models in $MODELS"
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
  echo "All there. Try: gen edit IMAGE \"instruction\""
  exit 0
fi

avail_gb=$(df -BG --output=avail "$MODELS" | tail -1 | tr -dc '0-9')
echo
echo "To download: about ${need} GB. Free on that disk: ${avail_gb} GB."
if awk -v a="$avail_gb" -v n="$need" 'BEGIN { exit !(a < n + 1) }'; then
  echo "!! Probably not enough space (a resumed *.part needs less)."
fi
echo "FLUX.1 [dev] is under a non-commercial licence: https://huggingface.co/black-forest-labs/FLUX.1-Kontext-dev"

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
echo "Then try: gen edit 'C:\\Users\\lalli\\Pictures\\lil-tulu.png' \"Make the character wave with one tentacle, seen from the side. Keep the same character design, colors and art style.\" -n 4 --open"
