# imagegen

Local image generation on **tulu** (AMD RX 9070 XT 16 GB, ROCm 7.2, WSL2 Ubuntu 24.04).
It is a thin driver around [ComfyUI](https://github.com/comfyanonymous/ComfyUI)'s HTTP API: presets in
`config.toml` fill the workflow templates in `workflows/`, and every run lands in a dated folder with PNGs,
JSON sidecars and an `index.html` contact sheet.

- ComfyUI runs as the systemd service `comfyui` on http://127.0.0.1:8188 (`journalctl -u comfyui -f` for logs)
- Outputs go to `output_dir` in `config.toml` (`C:\Users\lalli\Pictures\imagegen`)
- Developer notes and repo rules: [CLAUDE.md](CLAUDE.md)

## Quick start

```bash
./setup.sh                                     # venv, `gen` wrapper, upscaler + rembg models (safe to re-run)
gen doctor                                     # health check: ComfyUI, GPU, models, workflows
gen "misty fjord at sunrise" -p wallpaper --open
```

## `gen` CLI

| Command | What it does |
|---|---|
| `gen "prompt" [-p PRESET] [-n 4] [--open]` | Text to image with a preset (`gen presets` lists them) |
| `gen vary PATH [--mask M] [-s 0.4]` | img2img variations, or inpaint with a mask |
| `gen cutout PATH` | Remove the background (transparent PNG) |
| `gen edit PATH "instruction"` | Edit with a plain-language instruction; FLUX.1 Kontext, opt-in models (`models/flux-kontext.sh`) |
| `gen project new\|list\|status\|set\|gen\|promote\|keep` | Creative projects: versioned prompt/palette/refs, named assets |
| `gen recent`, `gen sheet [RUN_DIR\|DATE]` | Recent runs; rebuild contact sheets |
| `gen set-wallpaper PATH` | Set a Windows wallpaper |
| `gen doctor` | Read-only health check |

Presets: `draft` (1024², quick), `wallpaper` (16:9, upscaled to 3840x2160), `wallpaper_ultrawide`
(21:9, upscaled to 3440x1440), `icon` (centered object, background removed).

## MCP tools (Claude desktop app)

`mcp_server.py` exposes: `generate`, `get_job`, `vary`, `cutout`, `doctor`, `list_presets`, `recent_runs`,
`view_image`, `set_wallpaper`, `open_contact_sheet` and `project_*`. `generate` waits up to 45s, then returns
a `job_id` to poll with `get_job` (the remote bridge times out at about 60s). Restart the Claude app after
`mcp_server.py` changes. The config entry is printed at the end of `setup.sh`.

## Updating

```bash
~/imagegen/update.sh
```

It fetches, shows the incoming PRs and files, flags risky changes (sudo, systemd, `.wslconfig`, dependencies,
downloads), asks y/N, then fast-forwards to exactly what it showed. It reinstalls the venv only when
`requirements.txt` changed and tells you when to restart the Claude app. It never downloads models.

It remembers the last commit it applied, so if you `git pull` by hand first, the next `update.sh` still
shows those changes and runs their steps. `update.sh --since REV` re-runs the steps for everything after REV.

## Tests

```bash
./test.sh            # any args go to pytest, e.g. ./test.sh -k projects -x
```

No GPU, ComfyUI or tulu needed: it uses a fake ComfyUI server and stubs. CI runs it on every PR.

## Models

| Model | Used for | Used by | ComfyUI folder | Size on disk | Memory | Licence | Released | How it's downloaded |
|---|---|---|---|---|---|---|---|---|
| **SDXL base 1.0** (`sd_xl_base_1.0.safetensors`) | Generation, img2img, inpaint | every preset; `gen`, `gen vary`, `gen project gen` | `checkpoints` | 6.9 GB | ~8 GB VRAM at 1024² | CreativeML Open RAIL++-M | Jul 2023 | By hand from [stabilityai/stable-diffusion-xl-base-1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0) |
| **RealESRGAN x4plus** (`RealESRGAN_x4plus.pth`) | 4x upscale | `wallpaper`, `wallpaper_ultrawide` | `upscale_models` | 64 MB | ~1-2 GB VRAM (tiled) | BSD-3-Clause | Jul 2021 | `setup.sh` (GitHub release) |
| **rembg u2net** (`u2net.onnx`) | Background removal | `icon` preset, `gen cutout` | not ComfyUI: `~/.u2net/` | 176 MB | ~1 GB RAM, CPU only | Apache 2.0 (rembg: MIT) | 2020 | `setup.sh` (rembg fetches it on first use) |
| **FLUX.1 Kontext [dev] fp8** (`flux1-dev-kontext_fp8_scaled.safetensors`) | Instruction editing | `gen edit`, MCP `edit` | `diffusion_models` | 11.9 GB | ~12 GB VRAM | FLUX.1 [dev] Non-Commercial | Jun 2025 | `models/flux-kontext.sh` (opt-in, asks y/N) |
| ↳ T5-XXL encoder fp8 (`t5xxl_fp8_e4m3fn_scaled.safetensors`) | Kontext text encoder | same | `text_encoders` | 5.2 GB | held in RAM, swapped in | Apache 2.0 | 2020 (Google T5 v1.1) | same script |
| ↳ CLIP-L (`clip_l.safetensors`) | Kontext text encoder | same | `text_encoders` | 0.25 GB | small | MIT | Jan 2021 (OpenAI) | same script |
| ↳ FLUX VAE (`ae.safetensors`) | Kontext latent decode | same | `vae` | 0.33 GB | small | Apache 2.0 | Aug 2024 | same script |

So yes, editing uses a different model: SDXL makes and varies images, Kontext follows instructions
("make it wave a tentacle") while keeping the rest of the picture. The Kontext set is ~17.7 GB and is
never downloaded automatically. `gen doctor` shows which models are present (missing Kontext files
are only a warning, since they are opt-in).

Sizes and memory are approximate. VRAM figures are for the model weights at the preset sizes, not
measured on tulu.

### RAM: give WSL 24 GB for Kontext

Kontext needs about **17 GB of system RAM** on top of the 16 GB VRAM, because ComfyUI swaps the 12 GB
model and the 5 GB T5 encoder between RAM and VRAM. If ComfyUI gets killed mid-edit, WSL is out of memory.
Set this in `C:\Users\lalli\.wslconfig`, then run `wsl --shutdown` in PowerShell (stops ComfyUI too):

```ini
[wsl2]
memory=24GB
swap=16GB
```

This is what tulu runs since 2026-11-01. The first Kontext run on the default 16 GB was OOM-killed at
~15.5 GB, and `gen` then reports "ComfyUI restarted and lost the job".

`windows/install-wslconfig.sh` writes 50% of the PC's RAM by default, which may be less than that, and it
never overwrites an existing `.wslconfig`.

### Alternatives

From a model comparison on 2026-11-01 (web research, nothing tested on tulu yet):

- **FLUX.2 klein 4B** (Jan 2026): the likely lighter replacement for Kontext. Apache 2.0, ~8 GB, does
  both text-to-image and editing with multiple reference images, native ComfyUI support. It would remove the
  RAM pressure and the non-commercial licence. (The 9B variant is non-commercial.)
- **Qwen-Image-2.1** (Sep 2026): 7B, one checkpoint for generation and editing with up to 10 references,
  RGBA output. Research-only licence.
- **Qwen-Image-Edit-2511**: strong character consistency, Apache 2.0, but 20B, so it needs GGUF or offload on 16 GB.
- **FLUX.2 dev** (32B) is too big for 16 GB.

Plan: keep Kontext for now, then a bake-off (klein 4B vs Qwen-Image-2.1) on the lil tulu mascot before
switching. On the RX 9070 XT, use `fp8_scaled` checkpoints: `int8_convrot` ones render black on gfx1201.
