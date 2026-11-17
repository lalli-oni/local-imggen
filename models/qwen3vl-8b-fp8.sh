#!/usr/bin/env bash
# Opt-in download of the Qwen3-VL-8B text encoder in fp8 (scaled) into ~/comfyui/models: ~10.6 GB.
# Used by the `qwen_lite` preset (#51): Qwen-Image 2.1 with this encoder instead of the 16 GB bf16 one,
# so a Qwen run fits in WSL's RAM without swapping (the bf16 encoder locked up Windows during a video).
# Also lists the rest of Qwen-Image 2.1 (shared with qwen-image-2.1.sh): files already there are skipped,
# so with the `qwen` preset installed this downloads only the 10.6 GB encoder.
#   ~/imagegen/models/qwen3vl-8b-fp8.sh        # shows what is missing, asks y/N, downloads
#   ~/imagegen/models/qwen3vl-8b-fp8.sh -y     # no prompt
# Safe to re-run: skips files that are there, resumes partial downloads (*.part).
# Only writes model files. No sudo, no pip, never touches ~/venvs/comfy or the comfyui service.
# fp8_scaled, not int8_convrot (int8 renders black on the RX 9070 XT). From Comfy-Org's Qwen3-VL repack.
# Licence: Apache 2.0 (Qwen3-VL); the image model itself stays under the Qwen Research License.
set -euo pipefail

TITLE="Qwen3-VL-8B text encoder (fp8)"
# folder | file | path on huggingface.co | approx size in GB
FILES=(
  "diffusion_models|qwen_image_2.1_bf16.safetensors|Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_bf16.safetensors|13.25"
  "vae|qwen_image_2.1_vae_bf16.safetensors|Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors|0.64"
  "text_encoders|qwen3vl_8b_fp8_scaled.safetensors|Comfy-Org/Qwen3-VL/resolve/main/text_encoders/qwen3vl_8b_fp8_scaled.safetensors|10.6"
)
LICENCE="Qwen3-VL is Apache 2.0: https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct"
TRY="gen bakeoff --t2i --presets qwen,qwen_lite --count 1 --seed 1 \"a red fox in a yellow raincoat at a bus stop on a rainy night, storybook illustration\""
# shellcheck source=models/_fetch.sh
source "$(dirname "$0")/_fetch.sh"
