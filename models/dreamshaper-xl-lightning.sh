#!/usr/bin/env bash
# Opt-in download of DreamShaper XL Lightning (an SDXL finetune) into ~/comfyui/models: ~6.9 GB.
# Used by the `dreamshaper` preset: gen "prompt" -p dreamshaper, gen bakeoff --t2i (#51).
#   ~/imagegen/models/dreamshaper-xl-lightning.sh        # shows what is missing, asks y/N, downloads
#   ~/imagegen/models/dreamshaper-xl-lightning.sh -y     # no prompt
# Safe to re-run: skips files that are there, resumes partial downloads (*.part).
# Only writes model files. No sudo, no pip, never touches ~/venvs/comfy or the comfyui service.
# One checkpoint file (model + CLIP + VAE), loaded like sd_xl_base_1.0: no new nodes needed.
# Lightning = distilled for 4-8 steps at cfg ~2 with DPM++ SDE Karras (workflows/sdxl_lightning.json).
# Licence: CreativeML Open RAIL++-M (same as SDXL base).
set -euo pipefail

TITLE="DreamShaper XL Lightning (SDXL finetune)"
# folder | file | path on huggingface.co | approx size in GB
FILES=(
  "checkpoints|DreamShaperXL_Lightning.safetensors|Lykon/dreamshaper-xl-lightning/resolve/main/DreamShaperXL_Lightning.safetensors|6.94"
)
LICENCE="DreamShaper XL Lightning is CreativeML Open RAIL++-M: https://huggingface.co/Lykon/dreamshaper-xl-lightning"
TRY="gen \"a cute chibi green sea creature reading a book in a cosy window nook, storybook illustration\" -p dreamshaper -n 4 --open"
# shellcheck source=models/_fetch.sh
source "$(dirname "$0")/_fetch.sh"
