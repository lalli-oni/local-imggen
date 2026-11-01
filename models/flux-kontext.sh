#!/usr/bin/env bash
# Opt-in download of the models behind `gen edit` (FLUX.1 Kontext [dev], fp8) into ~/comfyui/models.
#   ~/imagegen/models/flux-kontext.sh        # shows what is missing, asks y/N, downloads
#   ~/imagegen/models/flux-kontext.sh -y     # no prompt
# Safe to re-run: skips files that are there, resumes partial downloads (*.part).
# Only writes model files. No sudo, no pip, never touches ~/venvs/comfy or the comfyui service
# (ComfyUI sees new model files without a restart).
# Licence: FLUX.1 [dev] Non-Commercial License (personal/non-commercial use).
set -euo pipefail

TITLE="FLUX.1 Kontext models"
# folder | file | path on huggingface.co | approx size in GB
FILES=(
  "diffusion_models|flux1-dev-kontext_fp8_scaled.safetensors|Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors|11.9"
  "text_encoders|t5xxl_fp8_e4m3fn_scaled.safetensors|comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors|5.2"
  "text_encoders|clip_l.safetensors|comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors|0.25"
  "vae|ae.safetensors|Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors|0.33"
)
LICENCE="FLUX.1 [dev] is under a non-commercial licence: https://huggingface.co/black-forest-labs/FLUX.1-Kontext-dev"
TRY="gen edit 'C:\\Users\\lalli\\Pictures\\lil-tulu.png' \"Make the character wave with one tentacle, seen from the side. Keep the same character design, colors and art style.\" -n 4 --open"
# shellcheck source=models/_fetch.sh
source "$(dirname "$0")/_fetch.sh"
