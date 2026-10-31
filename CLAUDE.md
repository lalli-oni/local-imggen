# imagegen

Local image generation: ComfyUI (systemd service `comfyui`, http://127.0.0.1:8188) on an AMD RX 9070 XT
via ROCm 7.2 in WSL2 (Ubuntu 24.04). This repo is a thin driver around ComfyUI's HTTP API.

## Layout
- `config.toml`: ComfyUI URL, `output_dir` (Windows side, `/mnt/c/Users/lalli/Pictures/imagegen`), presets
- `workflows/*.json`: ComfyUI **API-format** workflows; string values `"{{key}}"` are filled from preset/args
- `imagegen.py`: core + `gen` CLI (`gen --help`, `gen presets`, `gen recent`, `gen set-wallpaper PATH`)
- `mcp_server.py`: MCP tools (generate, list_presets, recent_runs, view_image, set_wallpaper, open_contact_sheet)
- `setup.sh`: idempotent setup (venv `~/venvs/imagegen`, `gen` wrapper, upscaler + rembg models)

## Rules
- Never `pip install` into `~/venvs/comfy` without excluding torch/torchvision/torchaudio/triton:
  it holds AMD's ROCm wheels and a generic torch would replace them.
- Models: `~/comfyui/models/{checkpoints,upscale_models,...}`.
- Adding a preset = new `[presets.x]` table in config.toml (+ a workflow JSON if the graph differs).
  Every `{{key}}` in the workflow must be a preset key, or one of: seed, prompt, negative, checkpoint.
- Outputs: `<output_dir>/<YYYY-MM-DD>/<HHMMSS>-<preset>-<slug>/` with PNGs, per-image JSON sidecars
  (prompt, seed, params), `run.json` and `index.html` contact sheet.
- Logs: `journalctl -u comfyui -f`.
