#!/usr/bin/env bash
# Opt-in download of the SDXL IP-Adapter models into ~/comfyui/models: ~3.4 GB.
# Used by the `sdxl_ref` and `dreamshaper_ref` presets: an SDXL model draws the character in a ref image
# (gen edit REF "scene" -p sdxl_ref, and the ref test of gen bakeoff --t2i, #51).
#   ~/imagegen/models/ipadapter-sdxl.sh        # shows what is missing, asks y/N, downloads
#   ~/imagegen/models/ipadapter-sdxl.sh -y     # no prompt
# Safe to re-run: skips files that are there, resumes partial downloads (*.part).
# Only writes model files. No sudo, no pip, never touches ~/venvs/comfy or the comfyui service.
# Also needs the ComfyUI_IPAdapter_plus custom node (IPAdapterModelLoader, IPAdapterAdvanced), which this
# script does NOT install. Once, by hand (no pip needed; restarting ComfyUI asks for sudo):
#   git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus ~/comfyui/custom_nodes/ComfyUI_IPAdapter_plus
#   sudo systemctl restart comfyui
# gen doctor then shows the nodes and models as ok.
# Licence: IP-Adapter is Apache 2.0; the CLIP ViT-H/14 image encoder (LAION) is MIT.
set -euo pipefail

TITLE="SDXL IP-Adapter models"
# folder | file | path on huggingface.co | approx size in GB
FILES=(
  "ipadapter|ip-adapter-plus_sdxl_vit-h.safetensors|h94/IP-Adapter/resolve/main/sdxl_models/ip-adapter-plus_sdxl_vit-h.safetensors|0.85"
  "clip_vision|CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors|h94/IP-Adapter/resolve/main/models/image_encoder/model.safetensors|2.53"
)
LICENCE="IP-Adapter is Apache 2.0: https://huggingface.co/h94/IP-Adapter"
TRY="gen edit 'C:\\Users\\lalli\\Pictures\\lil-tulu.png' \"lil tulu reading a book in a cosy window nook on a rainy evening\" -p sdxl_ref -n 4 --open"
if [ ! -d "${COMFY_MODELS:-$HOME/comfyui/models}/../custom_nodes/ComfyUI_IPAdapter_plus" ]; then
  echo "note: the ComfyUI_IPAdapter_plus node isn't installed yet (see: $0 --help)"
fi
# shellcheck source=models/_fetch.sh
source "$(dirname "$0")/_fetch.sh"
