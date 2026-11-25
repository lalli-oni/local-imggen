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
| `gen "prompt" --style NAME` / `gen styles` | A named look from `[styles]` in `config.toml` (repeatable; `--style none` = plain). The wallpaper presets default to `synthwave`, the house style |
| `gen crt PATH... [--lines 360] [--scanline 0.22] ...` | The CRT/VHS look on finished images (`NAME-crt.png`, the clean one stays): try knobs here, keep them in `[crt]` |
| `gen vary PATH [--mask M] [-s 0.4]` | img2img variations, or inpaint with a mask |
| `gen cutout PATH [--clean]` | Remove the background (transparent PNG); `--clean` also drops stray shapes and centres it on a square |
| `gen edit PATH "instruction" [-p klein\|kontext\|qwen]` | Edit with a plain-language instruction; FLUX.2 klein by default, opt-in models (`models/*.sh`) |
| `gen bakeoff PATH "instr" ["instr"...] [-p kontext,klein,qwen] [-n 2]` | The same edits through several edit models (same seeds), time/VRAM/RAM per model, one contact sheet |
| `gen bakeoff --t2i "fresh prompt"... [--ref IMG --ref-prompt "scene"...] [-p draft,dreamshaper,klein,qwen] [--steps klein=16]` | Compare text-to-image models: fresh prompts from text alone, and a character from a ref image (IP-Adapter for SDXL) |
| `gen sweep "prompt" [-p klein,qwen] [--steps 8,16 \| --steps klein=4,8,16 qwen=15,25,40] [--cfg 3,6] [--guidance G,H] [--style a,none] [--seeds N]` | One prompt over a grid of settings or models (same seeds in every cell) as a labelled grid on one contact sheet (#17) |
| `gen project new\|list\|status\|set\|gen\|promote\|keep\|import\|doc` | Creative projects: versioned prompt/palette/refs, named assets, docs, import an existing folder |
| `gen project rate\|note\|board` | The project's board: pick/maybe/reject and notes on pass images (`P:N`), and its `index.html` page |
| `gen board serve [SLUG] [--open]` | The board on http://127.0.0.1:8190, clickable: click an image to select it, then keys: arrows move, 1/2/3 rate, 0 clears, n notes, o opens full size, "Generate 4 more", refreshes itself on any change |
| `gen char new\|list\|status\|set` | Shared characters (lil tulu) that projects cast; a change flags the assets that show them |
| `gen recent`, `gen sheet [RUN_DIR\|DATE]` | Recent runs; rebuild contact sheets |
| `gen set-wallpaper PATH` | Set a Windows wallpaper |
| `gen daily [--show] [--no-set]` | Today's wallpaper (theme and cast from `[daily]` in `config.toml`), then set it. Schedule: `windows/daily-task.sh on [--at 06:00]` |
| `gen doctor` | Read-only health check |

While a run works, `gen` shows what runs and how far it is (#61). First a line with the preset's models, passes,
steps and sizes, then (in a terminal) a live block:

```
wallpaper: sd_xl_base_1.0 · 1 pass, 30 steps · 1344x768 -> 3840x2160 (RealESRGAN_x4plus) · 4 images
image 2/4  ✓ load  ✓ encode  ▶ sample 18/30  ○ decode  ○ upscale  ○ save
  ████████████░░░░░░░░  58%  1:52 elapsed · ~1:21 left
  VRAM ▓▓▓▓▓▓▓░░░ 11.2/16.0 GiB · RAM ▓▓▓▓▓░░░░░ 12.0/24.0 GiB
```

The ETA learns from the preset's last runs and from the images already done; VRAM and RAM turn yellow at 75%
and red at 90% of the ceiling (RAM's is the `.wslconfig` cap), and the run ends with the peak. Not drawn when
stderr is not a terminal (MCP, `gen daily`); `IMAGEGEN_PROGRESS=0` turns it off, `=1` forces it on.

Every run ends with a timing line, e.g. `timing: 4 images in 210.3s (first 80.1s, then ~43.4s each): load 3.0s,
sample 160.2s, decode 8.1s, upscale 30.0s, fetch 1.2s`. The numbers are also in `run.json` and in each sidecar
under `"timing"` (seconds per stage; the sidecar also lists every node). They come from ComfyUI's websocket
events. `load` is reading weights from disk; moving them onto the GPU is counted in `sample`, so the first
image after a model change is slower. `queue` is the wait before ComfyUI started on the job.

Presets: `draft` (SDXL, 1024², quick), `wallpaper` (Qwen-Image 2.1, 16:9, upscaled to 3840x2160),
`wallpaper_ultrawide` (Qwen, 21:9, upscaled to 3440x1440), `wallpaper_ref` (Qwen: a 4K 16:9 wallpaper of the
character in a ref image, `gen edit REF "scene" -p wallpaper_ref`), `wallpaper_cast` (Qwen, #52: the scene from
the prompt first, then a Qwen edit puts the characters from `--ref` images into it, about twice a wallpaper's time;
`gen daily` uses it), `icon` (SDXL, centered object, background removed). The wallpapers moved to Qwen after the #51 bake-off: about 3 minutes an image, so keep
the resource limits on (`comfy/limits.sh on`; `gen doctor` warns otherwise). The fast SDXL/klein versions are still
there: `wallpaper_sdxl`, `wallpaper_ultrawide_sdxl`, `wallpaper_ref_klein`. Also: `kontext` (edit only), `klein`,
`qwen`, `qwen_lite` (text-to-image and `gen edit -p ...`; no `gen vary`).

### Characters in a scene (wallpaper_cast)

```bash
gen "a small cute cartoon creature running a taco truck on the moon" -p wallpaper_cast --ref ~/tulu.png
gen "two small cartoon creatures at a karaoke bar" -p wallpaper_cast --ref tulu.png --ref cat.png \
    --cast-prompt "Replace the creature on the left with the character from image 2 and the one on the right with the character from image 3. Keep everything else."
```

One ComfyUI job per image: Qwen draws the scene, then edits it with the scene as image 1 and the refs as images
2, 3, ... (up to 15), then the 4x upscale. The scene is kept as `NN-seedS-scene.png`; the contact sheet shows the
refs and scenes first, and "vs scene" compares a result with its own scene. A stand-in in the scene ("a small
cartoon creature") that the edit replaces gives the character a place, pose and light that fit; `gen daily` does
this (`[daily] stand_in`, `scene_prompt`, `cast_prompt`).

### How a preset works, and how to see it

A preset is two parts: a `[presets.NAME]` table in `config.toml` (model files, size, steps, upscaler, ...) and a
ComfyUI workflow in `workflows/*.json` (API format) whose `"{{key}}"` slots `gen` fills from that table.

To see the graph that made an image, **drag the PNG onto ComfyUI** at http://127.0.0.1:8188: ComfyUI stores the
filled-in workflow inside every image it saves, so you get the exact nodes and values as boxes and wires, and can
tweak and re-run it there. This works for every run's PNGs except cut-outs (`icon`, `gen cutout`): removing the
background rewrites the file without that data (the run's non-transparent PNG still has it).

## MCP tools (Claude desktop app)

`mcp_server.py` exposes: `generate`, `get_job`, `vary`, `cutout`, `doctor`, `list_presets`, `recent_runs`,
`view_image`, `set_wallpaper`, `open_contact_sheet`, `project_*` and `character_*`. `generate` waits up to 45s, then returns
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
| **DreamShaper XL Lightning** (`DreamShaperXL_Lightning.safetensors`) | SDXL finetune, 6 steps | `dreamshaper` preset, `gen bakeoff --t2i` (#51) | `checkpoints` | 6.94 GB | ~8 GB VRAM, like SDXL | CreativeML Open RAIL++-M | Feb 2024 | `models/dreamshaper-xl-lightning.sh` (opt-in, asks y/N) |
| **IP-Adapter Plus SDXL** (`ip-adapter-plus_sdxl_vit-h.safetensors`) + CLIP ViT-H/14 image encoder | SDXL models draw the character in a ref image | `sdxl_ref`, `dreamshaper_ref` presets (#51) | `ipadapter`, `clip_vision` | 0.85 + 2.53 GB | small on top of SDXL | Apache 2.0 / MIT | 2023 | `models/ipadapter-sdxl.sh` (opt-in, asks y/N) + the ComfyUI_IPAdapter_plus node (by hand) |
| **RealESRGAN x4plus** (`RealESRGAN_x4plus.pth`) | 4x upscale | `wallpaper`, `wallpaper_ultrawide` | `upscale_models` | 64 MB | ~1-2 GB VRAM (tiled) | BSD-3-Clause | Jul 2021 | `setup.sh` (GitHub release) |
| **rembg u2net** (`u2net.onnx`) | Background removal | `icon` preset, `gen cutout` | not ComfyUI: `~/.u2net/` | 176 MB | ~1 GB RAM, CPU only | Apache 2.0 (rembg: MIT) | 2020 | `setup.sh` (rembg fetches it on first use) |
| **FLUX.1 Kontext [dev] fp8** (`flux1-dev-kontext_fp8_scaled.safetensors`) | Instruction editing | `gen edit -p kontext` | `diffusion_models` | 11.9 GB | ~12 GB VRAM | FLUX.1 [dev] Non-Commercial | Jun 2025 | `models/flux-kontext.sh` (opt-in, asks y/N) |
| ↳ T5-XXL encoder fp8 (`t5xxl_fp8_e4m3fn_scaled.safetensors`) | Kontext text encoder | same | `text_encoders` | 5.2 GB | held in RAM, swapped in | Apache 2.0 | 2020 (Google T5 v1.1) | same script |
| ↳ CLIP-L (`clip_l.safetensors`) | Kontext text encoder | same | `text_encoders` | 0.25 GB | small | MIT | Jan 2021 (OpenAI) | same script |
| ↳ FLUX VAE (`ae.safetensors`) | Kontext latent decode | same | `vae` | 0.33 GB | small | Apache 2.0 | Aug 2024 | same script |
| **FLUX.2 [klein] 4B fp8** (`flux-2-klein-4b-fp8.safetensors`) | Text-to-image + editing, 4 steps | `gen edit`, MCP `edit` (default), `klein` preset | `diffusion_models` | 4.07 GB | to measure | Apache 2.0 | Jan 2026 | `models/flux2-klein.sh` (opt-in, asks y/N) |
| ↳ Qwen3-4B encoder (`qwen_3_4b.safetensors`) | klein text encoder | same | `text_encoders` | 7.49 GB | with the unet: 10.7 GB VRAM peak, ~19 GB RAM (bake-off) | Apache 2.0 | 2025 | same script |
| ↳ FLUX.2 VAE (`flux2-vae.safetensors`) | klein latent decode | same | `vae` | 0.32 GB | small | Apache 2.0 | Nov 2025 | same script |
| **FLUX 3 Image Klein 5.6B bf16** (`flux3-image-klein-bf16.safetensors`) | Text-to-image + editing with refs, 16 steps | `klein3` preset (#95, comparison only) | `diffusion_models` | 11.1 GB | to measure | FLUX Kommunity (gated: HF licence + read token in `~/.cache/huggingface/token`) | Oct 2026 | `models/flux3-klein.sh` (opt-in, asks y/N) |
| ↳ Qwen3-VL-4B encoder (`qwen3vl_4b_bf16.safetensors`) | klein3 text encoder | same | `text_encoders` | 8.88 GB | to measure | same | 2026 | same script |
| **Qwen-Image-2.1** bf16, loaded as fp8 (`qwen_image_2.1_bf16.safetensors`) | Text-to-image + editing | `qwen` preset, `gen bakeoff` | `diffusion_models` | 13.25 GB | to measure | Qwen Research (non-commercial) | Sep 2026 | `models/qwen-image-2.1.sh` (opt-in, asks y/N) |
| **Qwen3-VL-8B text encoder** fp8 (`qwen3vl_8b_fp8_scaled.safetensors`) | Qwen-Image's text encoder at half the size | `qwen_lite` preset (#51) | `text_encoders` | 10.6 GB | ~6 GB less RAM than bf16 | Apache 2.0 | | `models/qwen3vl-8b-fp8.sh` (opt-in, asks y/N) |
| ↳ Qwen3-VL-8B encoder bf16 (`qwen3vl_8b_bf16.safetensors`) | Qwen text/image encoder | same | `text_encoders` | 16.33 GB | the RAM risk, to measure | Apache 2.0 | 2025 | same script |
| ↳ Qwen-Image-2.1 VAE (`qwen_image_2.1_vae_bf16.safetensors`) | Qwen latent decode | same | `vae` | 0.64 GB | small | Qwen Research | Sep 2026 | same script |

So yes, editing uses a different model: SDXL makes and varies images, klein (default since the #40 bake-off) follows instructions
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

From a model comparison on 2026-11-01 (web research, before the bake-off below):

- **FLUX.2 klein 4B** (Jan 2026): the likely lighter replacement for Kontext. Apache 2.0, ~8 GB, does
  both text-to-image and editing with multiple reference images, native ComfyUI support. It would remove the
  RAM pressure and the non-commercial licence. (The 9B variant is non-commercial.)
- **Qwen-Image-2.1** (Sep 2026): 7B, one checkpoint for generation and editing with up to 10 references,
  RGBA output. Research-only licence.
- **Qwen-Image-Edit-2511**: strong character consistency, Apache 2.0, but 20B, so it needs GGUF or offload on 16 GB.
- **FLUX.2 dev** (32B) is too big for 16 GB.

Bake-off result (#40, 2026-11-01 on tulu, lil tulu edits): klein ~7-16s per image once loaded (~1 min first),
VRAM 10.7 GB, and it followed "wave"; Kontext ~2 min per image and ignored "wave"; Qwen followed the text most
literally but took 2-9 min per image with RAM maxed. So klein is the default for `gen edit` and MCP `edit`. On the RX 9070 XT, use `fp8_scaled` checkpoints: `int8_convrot` ones render black on gfx1201.
That is why `qwen` uses Comfy-Org's bf16 files (its only other builds are int8_convrot) and loads the
diffusion model as fp8 with `weight_dtype`.

### Bake-off

```sh
~/imagegen/models/flux2-klein.sh && ~/imagegen/models/qwen-image-2.1.sh   # ~42 GB, both ask y/N
gen doctor                                                                # models + nodes (ComfyUI may need an update)
gen bakeoff 'C:\Users\lalli\Pictures\lil-tulu.png' \
  "Make the character wave with one tentacle. Keep the same character design, colors and art style." \
  "Show the character from the side. Keep the spiked tail, dark spots and both wings." \
  -n 2 --seed 1 --open
```

Each preset runs after ComfyUI unloads every model, so its first image includes the load. Peak VRAM is the
whole GPU (the desktop's share included), RAM/swap are the comfyui process and the system. A preset that fails
(missing models, OOM) is noted in the table and the rest still run. Results: one contact sheet with every image
tagged `preset · instruction`, and `bakeoff.json`.

### Text-to-image bake-off (#51)

Which model should replace SDXL base as the default? Two tests on one contact sheet, with the same seeds for
every model:

1. **Fresh prompt, no ref** (the positional prompts): every model draws the scene from text alone, all at
   1344x768 (`--size`). This shows general quality.
2. **lil tulu with the ref** (`--ref IMAGE --ref-prompt "..."`): every model draws the character from the ref image
   into the scene. klein and Qwen take the ref directly. SDXL base and DreamShaper take it through IP-Adapter
   (presets `sdxl_ref`, `dreamshaper_ref`, picked by `ref_preset`). Their size follows the ref, which is
   1024x1024 for the square lil tulu ref. This shows consistency. A line is added to each ref prompt saying the
   character appears once, in a new pose, and the scene fills the frame.

Each image is labelled with the model and its steps, for example "FLUX.2 klein 4B · 16 steps · ref · 2", where
2 is the prompt number. Each model uses its preset's steps: SDXL 25, DreamShaper 6, klein 4, Qwen 25.
`--steps klein=16` overrides one; klein's 4 steps gave many small quirks on lil tulu. `--steps klein=8/16/32`
sweeps them: a column per step count, to find where more steps stop paying off. `--count` is the number of
images per prompt and model, each with a new seed. It is not steps.

The sheet is rewritten after every column, so you can watch a long bake-off fill in (refresh the page). Ctrl-C
keeps it: the running column is marked "interrupted" and the rest "not run". A slow model that makes ComfyUI
swap no longer counts as lost: the client waits up to an hour per image, and gives up after 90s only when
ComfyUI refuses the connection (a crash). Ctrl-C or a timeout also cancels the jobs that run had queued in
ComfyUI, and a bake-off won't start while ComfyUI is busy with other jobs (it prints the commands to clear the
queue), since those would land in its timings. If ComfyUI sends nothing at all for 20 minutes, its worker is
dead (on tulu a ROCm `HSA exception: MemoryRegion::BlockAllocator::alloc failed` did that: the server kept
accepting jobs and never ran them), so the run stops and prints the `journalctl` and `sudo systemctl restart
comfyui` commands. A bake-off folder stopped before this had no sheet;
`gen bakeoff --rebuild FOLDER` makes one from its images (times from the sidecars, no memory numbers).

Qwen sometimes renders an all-black image when RAM runs out (seed 1 black, seed 2 fine, same run). Every
`gen` run now checks for that: a black image is rendered again with the same seed (the log says so), and one
that is still black is kept but marked `"blank": true`. `gen daily` and `gen --wallpaper` never set a blank image
as the wallpaper.

```sh
~/imagegen/models/dreamshaper-xl-lightning.sh   # ~6.9 GB, asks y/N
~/imagegen/models/ipadapter-sdxl.sh             # ~3.4 GB, asks y/N; its --help shows the one-time node install
gen doctor
gen bakeoff --t2i \
  --presets draft,dreamshaper,klein,qwen \
  --steps klein=16 \
  --count 2 \
  --seed 1 \
  --ref 'C:\Users\lalli\Pictures\lil-tulu.png' \
  --ref-prompt "lil tulu reading a book in a cosy window nook on a rainy evening, warm lamp light, cosy whimsical storybook illustration" \
  --open \
  "a red fox in a yellow raincoat holding a blue umbrella at a bus stop on a rainy street at night, a lit sign above it reads BUS 14, three puddles reflecting neon lights, a cat watching from a window, detailed storybook illustration"
```

### Parameter sweep grid (`gen sweep`, #17)

One prompt, many settings, one labelled grid. Every option you give several comma-separated values is an axis, and
the images are the product, all with the same prompt and seeds:

- `-p klein,qwen`: the model (a preset: its model and workflow). This is how the model-consolidation pass (#64) is
  compared. Use `--style none` so every model gets the bare prompt; without `--style` each preset keeps its own look.
- `--steps 4,8,16` (every model) or per model: `--steps klein=4,8,16 --steps qwen=15,25,40` (#94; repeat the flag). Each model then gets its own
  step counts, so a comparison can use each model's best setting. A model you don't name keeps its own default, and
  a bare list covers the others. When the lists differ, each model's steps are its own
  columns and the rows default to the seed.
- `--cfg` (SDXL, Qwen), `--guidance` (only for workflows with a guidance input; no text-to-image preset has one yet, so it errors), `--style a,none,b+c`, `--seeds N` (base, base+1, ...).

With one axis the values run along the top. With more, the rows are `--rows AXIS` (default: seed if it varies, else
the first of model, style, steps, cfg, guidance) and the columns are the rest, combined ("klein · steps 8").
A model with no input for a setting (klein has no cfg) renders that column once; its other values are shown as
skipped. More than 16 images asks first (`-y` to skip the question; without a terminal it refuses). The models are
unloaded between presets, each image has its time and peak VRAM/RAM in a collapsed table above the grid, and the
sheet is rewritten after every image, so Ctrl-C keeps what ran. The MCP tool `sweep` does the same (poll `get_job`).

```bash
# the first model comparison for #64: same prompt, same 2 seeds, klein vs Qwen (about 4 min per Qwen image)
gen sweep "a cozy cabin by a misty fjord at dusk, northern lights" -p klein,qwen --style none --seeds 2 --seed 100 --size 1344x768 --open
# how many steps are enough on klein and Qwen
gen sweep "a cozy cabin by a misty fjord at dusk" -p klein,qwen --style none --steps 4,8,16 --seed 100 --rows steps
```

