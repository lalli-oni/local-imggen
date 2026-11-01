#!/usr/bin/env bash
# Opt-in download of Qwen-Image-2.1 (7B, bf16 files) into ~/comfyui/models: ~30.2 GB.
# Used by the `qwen` preset: gen "prompt" -p qwen, gen edit IMAGE "..." -p qwen, gen bakeoff.
#   ~/imagegen/models/qwen-image-2.1.sh        # shows what is missing, asks y/N, downloads
#   ~/imagegen/models/qwen-image-2.1.sh -y     # no prompt
# Safe to re-run: skips files that are there, resumes partial downloads (*.part).
# Only writes model files. No sudo, no pip, never touches ~/venvs/comfy or the comfyui service.
# bf16 on purpose: Comfy-Org's smaller builds are int8_convrot, which renders black on the RX 9070 XT.
# The preset loads the 13 GB diffusion model as fp8 (weight_dtype), so VRAM use is about half;
# the 16 GB Qwen3-VL-8B text encoder stays bf16 and is the RAM risk (needs .wslconfig 24 GB + swap).
# Needs ComfyUI 0.37 or newer (TextEncodeQwenImage21, QwenImage21Cache): gen doctor checks the nodes.
# Licence: Qwen Research License (non-commercial).
set -euo pipefail

TITLE="Qwen-Image-2.1 models"
# folder | file | path on huggingface.co | approx size in GB
FILES=(
  "diffusion_models|qwen_image_2.1_bf16.safetensors|Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_bf16.safetensors|13.25"
  "text_encoders|qwen3vl_8b_bf16.safetensors|Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_bf16.safetensors|16.33"
  "vae|qwen_image_2.1_vae_bf16.safetensors|Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors|0.64"
)
LICENCE="Qwen-Image-2.1 is under the Qwen Research License (non-commercial): https://huggingface.co/Qwen/Qwen-Image-2.1"
TRY="gen edit 'C:\\Users\\lalli\\Pictures\\lil-tulu.png' \"Make the character wave with one tentacle. Keep the same character design, colors and art style.\" -p qwen -n 4 --open"
# shellcheck source=models/_fetch.sh
source "$(dirname "$0")/_fetch.sh"
