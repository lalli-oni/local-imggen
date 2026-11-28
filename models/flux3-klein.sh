#!/usr/bin/env bash
# Opt-in download of FLUX 3 Image Klein 5.6B (distilled, bf16) into ~/comfyui/models: ~20 GB.
# Used by the `klein3` preset (#95): gen "prompt" -p klein3, gen edit IMAGE "..." -p klein3, gen sweep/bakeoff.
#   ~/imagegen/models/flux3-klein.sh        # shows what is missing, asks y/N, downloads
#   ~/imagegen/models/flux3-klein.sh -y     # no prompt
# GATED repo: log in on huggingface.co, accept the licence on the model page, make a read token and save it once:
#   mkdir -p ~/.cache/huggingface && read -rsp "HF token: " t && echo "$t" > ~/.cache/huggingface/token && chmod 600 ~/.cache/huggingface/token
# (or HF_TOKEN=hf_... for one run). Safe to re-run: skips files that are there, resumes partial downloads (*.part).
# Only writes model files. No sudo, no pip, never touches ~/venvs/comfy or the comfyui service.
# Needs ComfyUI >= v0.41 (native FLUX 3 klein support): gen doctor checks the nodes.
# Not downloaded on purpose: the *-int8-convrot files (render black on the RX 9070 XT, gfx1201), nvfp4 (Blackwell),
# the *base* files (50 steps, slower) and the 9B prompt-upsampler text encoders.
# Licence: FLUX Kommunity licence (free under $5M revenue; personal use is fine).
set -euo pipefail

TITLE="FLUX 3 Image Klein models"
REPO="black-forest-labs/flux-3-image-klein-comfy"
# folder | file | path on huggingface.co | approx size in GB
FILES=(
  "diffusion_models|flux3-image-klein-bf16.safetensors|$REPO/resolve/main/diffusion_models/flux3-image-klein-bf16.safetensors|11.1"
  "text_encoders|qwen3vl_4b_bf16.safetensors|$REPO/resolve/main/text_encoders/qwen3vl_4b_bf16.safetensors|8.88"
  "vae|flux2-vae.safetensors|$REPO/resolve/main/vae/flux2-vae.safetensors|0.34"
)
LICENCE="FLUX 3 Image Klein is gated (FLUX Kommunity licence): accept it at https://huggingface.co/$REPO and save a read token in ~/.cache/huggingface/token first."
TRY="gen \"a lighthouse on a fjord at dusk, synthwave\" -p klein3 --open"
# shellcheck source=models/_fetch.sh
source "$(dirname "$0")/_fetch.sh"
