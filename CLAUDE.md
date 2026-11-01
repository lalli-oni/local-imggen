# imagegen

Local image generation: ComfyUI (systemd service `comfyui`, http://127.0.0.1:8188) on an AMD RX 9070 XT
via ROCm 7.2 in WSL2 (Ubuntu 24.04). This repo is a thin driver around ComfyUI's HTTP API.

## Layout
- `config.toml`: ComfyUI URL, `output_dir` (Windows side, `/mnt/c/Users/lalli/Pictures/imagegen`), presets
- `workflows/*.json`: ComfyUI **API-format** workflows; string values `"{{key}}"` are filled from preset/args
- `imagegen.py`: core + `gen` CLI (`gen --help`, `gen presets`, `gen recent`, `gen set-wallpaper PATH`,
  `gen vary PATH [--mask M] [-s 0.4]` for img2img/inpaint, `gen edit PATH "instruction"` for instruction edits
  (FLUX.1 Kontext, `kontext` preset), `gen cutout PATH [--clean]` for background removal,
  `gen sheet [RUN_DIR|DATE]` to rebuild contact sheets (default: latest run), `gen doctor` for a health check)
- `templates/contact_sheet.html`: the per-run `index.html` viewer (fit-all / size slider, A/B compare).
  Static, no build, opened via file://; `write_contact_sheet` inlines the run data as JSON.
- `mcp_server.py`: MCP tools (generate, get_job, vary, edit, cutout, doctor, list_presets, recent_runs, view_image, set_wallpaper,
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
  It applies steps from the last applied commit (`.git/imagegen-applied`), so a manual `git pull` is still caught; `--since REV` overrides.
- `windows/install-wslconfig.sh`: writes `%USERPROFILE%\.wslconfig` (cores/RAM for WSL). Manual, never overwrites,
  always-on once applied (needs `wsl --shutdown`), so it is not part of the on/off toggle.
- `models/flux-kontext.sh`: opt-in download (~17.7 GB, asks y/N) of the Kontext fp8 model + Flux text encoders
  and VAE into `~/comfyui/models`. Never run automatically; `gen doctor` shows them as warn while missing.
- `test.sh` + `tests/`: the test suite (see Testing below). `requirements-dev.txt` / `requirements-browser.txt`
  hold test-only deps; CI runs it on every PR (`.github/workflows/test.yml`).
- `setup.sh`: idempotent setup (venv `~/venvs/imagegen`, `gen` wrapper, upscaler + rembg models)

## Rules
- Never `pip install` into `~/venvs/comfy` without excluding torch/torchvision/torchaudio/triton:
  it holds AMD's ROCm wheels and a generic torch would replace them.
- Models: `~/comfyui/models/{checkpoints,upscale_models,...}`.
- Adding a preset = new `[presets.x]` table in config.toml (+ a workflow JSON if the graph differs).
  Every `{{key}}` in the workflow must be a preset key, or one of: seed, prompt, negative, checkpoint,
  and for img2img/inpaint workflows also image, mask, denoise (width/height then come from the source).
  A preset picks those graphs with `img2img_workflow` / `inpaint_workflow` (defaults: sdxl_img2img.json, sdxl_inpaint.json).
  Post steps (`post = [...]`, see `POST` in imagegen.py) run on each output in order: `remove_bg`, then the
  optional `keep_largest` (drop stray shapes) and `trim` (crop + centre on a square of the shorter side); the last two rewrite the
  transparent PNG in place and skip images without transparency.
  An edit preset has `edit_workflow` (gets image, width/height; no checkpoint/negative); without `workflow` it is edit-only.
- Outputs: `<output_dir>/<YYYY-MM-DD>/<HHMMSS>-<preset>-<slug>/` with PNGs, per-image JSON sidecars
  (prompt, seed, params), `run.json` and `index.html` contact sheet.
- Logs: `journalctl -u comfyui -f`.

## Testing
Run `./test.sh` before every push (any args go to pytest, e.g. `./test.sh -k projects -x`). It needs no tulu,
GPU or ComfyUI: the first run makes `.venv-test/` from requirements-dev.txt, and every test is isolated
(temp output_dir, IMAGEGEN_COMFY_URL on a closed port, rembg stubbed, `tests/stubs/` first on PATH).
Cloud sessions also run the contact sheet in Chromium (`/opt/pw-browsers`); elsewhere add `--browser`.

What's there to reuse, instead of building a one-off setup:
- `comfy` fixture (`tests/fake_comfy.py`): a fake ComfyUI HTTP server with canned PNGs. Inspect `comfy.workflows()`,
  `comfy.uploads`; switch on `reject`, `fail_jobs`, `no_gpu`, `delay` (slow jobs), `queue_running/pending`.
- `make_run` (a finished run folder: PNGs, sidecars, run.json), `make_image`, `out_dir`, `fake_rembg`, `stub_calls()`
  (what the fake wslpath/powershell.exe/explorer.exe/systemctl/sudo were called with).
- Shell scripts: `tests/shell_helpers.run_script()` runs a script with no terminal (y/N prompts read "no")
  or with `answer="y"` on a pseudo-terminal. See `test_limits_sh.py` (fake systemd drop-in dir) and
  `test_update_sh.py` (scratch git remote, PRs landed as GitHub-style merge commits).

How a thread tests a change:
1. Write the test plan first: what could break, and which of it the suite can check.
2. Add or update tests for new code and for each bug fixed, in the same PR. Extend the fakes rather than
   mocking around them (e.g. a new ComfyUI endpoint goes into fake_comfy.py).
3. `./test.sh` green, and CI green on the PR.
4. Test by hand only what can't reasonably be automated, and list it in the PR under "Needs a check on tulu"
   for Þór: image quality and model output on the real GPU, timings/VRAM, real systemd/sudo/WSL behaviour,
   Windows integration (wallpaper, explorer, toasts), the Claude desktop app picking up MCP changes.
