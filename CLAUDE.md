# imagegen

Local image generation: ComfyUI (systemd service `comfyui`, http://127.0.0.1:8188) on an AMD RX 9070 XT
via ROCm 7.2 in WSL2 (Ubuntu 24.04). This repo is a thin driver around ComfyUI's HTTP API.

## Layout
- `config.toml`: ComfyUI URL, `output_dir` (Windows side, `/mnt/c/Users/lalli/Pictures/imagegen`), presets
- `workflows/*.json`: ComfyUI **API-format** workflows; string values `"{{key}}"` are filled from preset/args
- `imagegen.py`: core + `gen` CLI (`gen --help`, `gen presets`, `gen recent`, `gen set-wallpaper PATH`,
  `gen vary PATH [--mask M] [-s 0.4]` for img2img/inpaint, `gen cutout PATH` for background removal,
  `gen sheet [RUN_DIR|DATE]` to rebuild contact sheets (default: latest run), `gen doctor` for a health check)
- `templates/contact_sheet.html`: the per-run `index.html` viewer (fit-all / size slider, A/B compare).
  Static, no build, opened via file://; `write_contact_sheet` inlines the run data as JSON.
- `mcp_server.py`: MCP tools (generate, get_job, vary, cutout, doctor, list_presets, recent_runs, view_image, set_wallpaper,
  open_contact_sheet, project_*)
- `projects.py`: creative projects (#22) behind `gen project new|list|status|set|gen|promote|keep` and the MCP
  `project_*` tools. `<output_dir>/projects/<slug>/project.json` holds the state (prompt, snippets, params,
  palette, refs), a version per change, named assets with history, and passes (tagged `gen` runs).
  Assets go stale when a later version changes prompt/negative/snippets/palette/refs.
- `doctor.py`: read-only health checks behind `gen doctor` and the MCP `doctor` tool
- `comfy/`: opt-in resource limits for the comfyui service, **off by default**. `comfy/limits.sh on|off|status`
  installs/removes `/etc/systemd/system/comfyui.service.d/zz-imagegen-limits.conf` built from `comfyui-args`
  (extra CLI flags) + `limits.conf` (Nice/CPUWeight/IOWeight). on/off restarts comfyui (sudo) after a y/N prompt
  that warns if a job is running (`-y` skips it; no terminal = no).
  `update.sh` runs `comfy/install.sh` on comfy/ changes, which re-applies only if limits are already on.
- `windows/install-wslconfig.sh`: writes `%USERPROFILE%\.wslconfig` (cores/RAM for WSL). Manual, never overwrites,
  always-on once applied (needs `wsl --shutdown`), so it is not part of the on/off toggle.
- `setup.sh`: idempotent setup (venv `~/venvs/imagegen`, `gen` wrapper, upscaler + rembg models)

## Rules
- Never `pip install` into `~/venvs/comfy` without excluding torch/torchvision/torchaudio/triton:
  it holds AMD's ROCm wheels and a generic torch would replace them.
- Models: `~/comfyui/models/{checkpoints,upscale_models,...}`.
- Adding a preset = new `[presets.x]` table in config.toml (+ a workflow JSON if the graph differs).
  Every `{{key}}` in the workflow must be a preset key, or one of: seed, prompt, negative, checkpoint,
  and for img2img/inpaint workflows also image, mask, denoise (width/height then come from the source).
  A preset picks those graphs with `img2img_workflow` / `inpaint_workflow` (defaults: sdxl_img2img.json, sdxl_inpaint.json).
- Outputs: `<output_dir>/<YYYY-MM-DD>/<HHMMSS>-<preset>-<slug>/` with PNGs, per-image JSON sidecars
  (prompt, seed, params), `run.json` and `index.html` contact sheet.
- Logs: `journalctl -u comfyui -f`.
