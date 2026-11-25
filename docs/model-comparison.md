# Model comparison: one model family for most workflows (#95, part of #64)

Goal: pick **one model family** for wallpapers, edits, explore and drafts, so one character LoRA (and maybe a
vaporwave style LoRA) covers them. This file keeps the evidence so anyone can reuse or redo it. Candidates and
newer models: #75. Numbers are from one machine, so compare them with each other, not with yours.

## Test machine
Windows 11 + WSL2 (Ubuntu 24.04), AMD RX 9070 XT 16 GB (gfx1201), ROCm 7.2, ComfyUI v0.41.0-19, WSL RAM ~24 GB +
16 GB swap, resource limits on (`comfy/limits.sh on`). Quality first: up to about 5 minutes an image is fine.

## Candidates
| Preset | Model | Files (disk) | Steps | Licence | Install |
|---|---|---|---|---|---|
| `klein` | FLUX.2 klein 4B fp8 | 3.8 GB + 7.5 GB encoder | 4 (16 for fewer quirks) | Apache-2.0 | `models/flux2-klein.sh` |
| `klein3` | FLUX 3 Image Klein 5.6B bf16 (no fp8 exists) | 11.1 GB + 8.9 GB encoder | 16, no CFG | FLUX Kommunity (free under $5M), gated on Hugging Face | `models/flux3-klein.sh` |
| `qwen` | Qwen-Image 2.1 bf16 | 14 GB + 17 GB encoder | 25 | Qwen Research (non-commercial) | `models/qwen-image-2.1.sh` |

## Test set (fixed: every model gets exactly this, and a new model later gets run on it too)
Same prompts, seeds (1, 2) and size (1344x768) for every model, all in one session so timings compare.
Each model runs at its own steps (klein 4 and 16, klein3 16, qwen 25).

| # | Prompt | Style | What it tests |
|---|---|---|---|
| 1 | `a cozy cabin by a misty fjord at dusk` | none | The baseline from the first klein vs qwen sweep (2026-11-24); plain scene, mood, light |
| 2 | `a crowded 80s arcade at night, kids at the cabinets, a cat asleep on a counter, a claw machine, a neon sign that says "GAME ON"` | `synthwave` (the house style) | Busy scene in the house look; sign text; funny details |
| 3 | `a busy harbour market at dawn, photorealistic documentary photograph, fishermen, crates of fish, gulls, a tilted rowing boat, many people` | none | Busy scene, realistic; people, hands, crowds |
| 4 | character from a reference image (tulu) | | Character fidelity: `gen bakeoff --t2i ... --ref IMG --ref-prompt "scene"` |

## How to rerun
```
gen sweep "a cozy cabin by a misty fjord at dusk" -p klein,klein3,qwen --steps klein=4,16 --steps klein3=16 --steps qwen=25 --seeds 2 --size 1344x768
gen sweep "a crowded 80s arcade at night, kids at the cabinets, a cat asleep on a counter, a claw machine, a neon sign that says \"GAME ON\"" -p klein,klein3,qwen --steps klein=4,16 --steps klein3=16 --steps qwen=25 --seeds 2 --size 1344x768 --style synthwave
gen sweep "a busy harbour market at dawn, photorealistic documentary photograph, fishermen, crates of fish, gulls, a tilted rowing boat, many people" -p klein,klein3,qwen --steps klein=4,16 --steps klein3=16 --steps qwen=25 --seeds 2 --size 1344x768
```
The sheet (`index.html` in each run folder) and the table `gen sweep` prints give time and peak VRAM/RAM per cell.

## Results
Fill from the tables `gen sweep` prints (seconds per image; the first image includes the model load).

| Scene | Model @ steps | first (s) | median (s) | peak VRAM GiB | peak RAM GiB | Notes |
|---|---|---|---|---|---|---|
| 1 cabin | klein @ 4 | | | | | |
| 1 cabin | klein @ 16 | | | | | |
| 1 cabin | klein3 @ 16 | | | | | |
| 1 cabin | qwen @ 25 | | | | | |
| 2 arcade | (same four rows) | | | | | |
| 3 market | (same four rows) | | | | | |

### Baseline impressions (2026-11-24, fjord prompt, before klein3)
- **klein 4B**: warmer, illustration-like, fewer notable artifacts. About 7 s warm, 4 steps.
- **Qwen-Image 2.1**: darker and more realistic, but "still feels off". About 3 minutes an image, RAM maxed.
- Both showed artifacts.

### Written assessment
Per model: look (vaporwave/synthwave fit, warmth, artifacts), prompt following and signs/text, character fidelity
with a ref (`gen edit`, `wallpaper_cast`), reliability (black images, crashes, swapping). To fill in after the runs.

## LoRA support and tooling
A LoRA works for one model family only, so this can outweigh a small quality gap. Plan: a character LoRA used in
masked inpaint/edit passes (#56), maybe a style LoRA for the scene (not a replacement for the CRT post step).

| Family | Loads in ComfyUI | Training on a 16 GB RX 9070 XT (WSL2, ROCm 7.2) | Existing LoRAs |
|---|---|---|---|
| FLUX.2 klein 4B | `LoraLoaderModelOnly` (to verify) | ai-toolkit guide for RX 9070 in WSL2 exists, Flux "maybe"; musubi-tuner klein LoRAs shown on AMD Strix Halo | to check |
| FLUX 3 klein 5.6B | to verify | none found yet (see #75) | to check |
| Qwen-Image 2.1 | to verify | guides assume 24 GB+; option: train on a rented GPU, run locally | to check |

## Decision
Not made yet. Record the pick, the reason and the date here, then switch the presets (#64 A-C, explore stage,
the cast pass) and update #75.
