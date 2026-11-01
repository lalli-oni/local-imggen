#!/usr/bin/env bash
# Opt-in download of FLUX.2 [klein] 4B (distilled, fp8) into ~/comfyui/models: ~11.9 GB.
# Used by the `klein` preset: gen "prompt" -p klein, gen edit IMAGE "..." -p klein, gen bakeoff.
#   ~/imagegen/models/flux2-klein.sh        # shows what is missing, asks y/N, downloads
#   ~/imagegen/models/flux2-klein.sh -y     # no prompt
# Safe to re-run: skips files that are there, resumes partial downloads (*.part).
# Only writes model files. No sudo, no pip, never touches ~/venvs/comfy or the comfyui service.
# Needs a ComfyUI from 2026 (Flux2Scheduler, EmptyFlux2LatentImage): gen doctor checks the nodes.
# Licence: Apache 2.0 (FLUX.2 [klein] 4B); the text encoder is Qwen3-4B (Apache 2.0).
set -euo pipefail

TITLE="FLUX.2 klein 4B models"
# folder | file | path on huggingface.co | approx size in GB
FILES=(
  "diffusion_models|flux-2-klein-4b-fp8.safetensors|black-forest-labs/FLUX.2-klein-4b-fp8/resolve/main/flux-2-klein-4b-fp8.safetensors|4.07"
  "text_encoders|qwen_3_4b.safetensors|Comfy-Org/flux2-klein/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors|7.49"
  "vae|flux2-vae.safetensors|Comfy-Org/flux2-dev/resolve/main/split_files/vae/flux2-vae.safetensors|0.32"
)
LICENCE="FLUX.2 [klein] 4B is Apache 2.0: https://huggingface.co/black-forest-labs/FLUX.2-klein-4B"
TRY="gen edit 'C:\\Users\\lalli\\Pictures\\lil-tulu.png' \"Make the character wave with one tentacle. Keep the same character design, colors and art style.\" -p klein -n 4 --open"
# shellcheck source=models/_fetch.sh
source "$(dirname "$0")/_fetch.sh"
