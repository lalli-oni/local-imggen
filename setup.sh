#!/usr/bin/env bash
# One-time setup for imagegen. Safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HOME/venvs/imagegen"
COMFY="$HOME/comfyui"

echo "==> venv for imagegen (separate from ComfyUI's, so it can't touch your ROCm torch)"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$HERE/requirements.txt"

echo "==> 'gen' command in ~/.local/bin"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/gen" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python" "$HERE/imagegen.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/gen"

echo "==> upscaler model (RealESRGAN x4, ~65 MB)"
UP="$COMFY/models/upscale_models/RealESRGAN_x4plus.pth"
mkdir -p "$(dirname "$UP")"
[ -f "$UP" ] || wget -q --show-progress -O "$UP" \
  https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth

echo "==> background-removal model (first-use download, ~170 MB)"
"$VENV/bin/python" -c "from rembg import new_session; new_session('u2net')" >/dev/null

echo "==> ComfyUI resource limits (systemd drop-in, asks for sudo)"
"$HERE/comfy/install.sh"

echo "==> output folder"
OUT="$("$VENV/bin/python" -c "import sys; sys.path.insert(0, '$HERE'); import imagegen; print(imagegen.load_config()['output_dir'])")"
mkdir -p "$OUT" && echo "    $OUT"

echo "==> MCP server import check"
"$VENV/bin/python" -c "import sys; sys.path.insert(0, '$HERE'); import mcp_server" && echo "    ok"

cat <<EOF

Done. Open a NEW terminal (so ~/.local/bin is on PATH), then try:
  gen presets
  gen "misty fjord at sunrise" -p wallpaper --open

Claude desktop MCP config entry (Settings -> Developer -> Edit Config):
  "imagegen": {
    "command": "wsl.exe",
    "args": ["-d", "${WSL_DISTRO_NAME:-Ubuntu-24.04}", "--cd", "$HERE", "-e", "$VENV/bin/python", "$HERE/mcp_server.py"]
  }
EOF
