# imagegen

Local image generation: ComfyUI (systemd service `comfyui`, http://127.0.0.1:8188) on an AMD RX 9070 XT
via ROCm 7.2 in WSL2 (Ubuntu 24.04). This repo is a thin driver around ComfyUI's HTTP API.

## Layout
- `config.toml`: ComfyUI URL, `output_dir` (Windows side, `/mnt/c/Users/lalli/Pictures/imagegen`), presets
- `workflows/*.json`: ComfyUI **API-format** workflows; string values `"{{key}}"` are filled from preset/args
- `imagegen.py`: core + `gen` CLI (`gen --help`, `gen presets`, `gen recent`, `gen set-wallpaper PATH`,
  `gen vary PATH [--mask M] [-s 0.4]` for img2img/inpaint, `gen edit PATH "instruction"` for instruction edits
  (FLUX.2 klein by default since the #40 bake-off, `EDIT_PRESET`; `-p kontext` / `-p qwen`), `gen cutout PATH [--clean]` for background removal,
  `gen bakeoff PATH "instr"...` (bakeoff.py: same edits through several edit presets, time/VRAM/RAM, one sheet;
  `--t2i "fresh prompt"... [--ref IMG --ref-prompt "scene"...]` compares text-to-image models, #51: fresh prompts from text,
  and the ref test through each preset's edit_workflow or its `ref_preset` (SDXL -> sdxl_ref, IP-Adapter); labels from `model`;
  the sheet is rewritten after every column and on Ctrl-C, `--rebuild FOLDER` for older stopped folders),
  `gen sheet [RUN_DIR|DATE]` to rebuild contact sheets (default: latest run), `gen doctor` for a health check)
- Styles (#10): `[styles.NAME]` in config.toml (`suffix`, optional `negative_extra`) is a look added after the preset
  suffix; a preset's `styles = [...]` is its default (the wallpaper presets: `synthwave`, the house style), and
  `generate(styles=[...])` / `gen --style NAME|none` / MCP `generate(styles=)` replace it (`[]` = none). Sidecars and
  run.json record `styles`; `gen vary` reuses the image's (none for sidecars from before #10). `gen styles`, MCP
  `list_styles`. A project snippet `"@NAME"` copies the style's prompt text (so the project versions it), so project
  passes and gen bakeoff pass `styles=[]`. `[daily] style`: style names ("" = the preset's) or a phrase of literal
  text; with a ref it goes into ref_prompt's {scene} (and `styles=[]`, its styles' post steps via `post=`), text-only
  presets get it from generate(). A style's `post = [...]` steps run after the preset's (`style_post`).
- `crt` post step: the CRT/VHS look on a finished image, Pillow only (soft scanlines, blur, colour bleed, glow, warmth,
  vignette, #74: lines that follow brightness (`beam`), VHS `wobble`, `roll` bands, `noise` grain, lines bowed (`curve`)
  and stronger at the edges (`edge`), soft corners (`edge_blur`); knobs in `[crt]`, `CRT_DEFAULTS`, sizes per 1080 lines). Writes `<stem>-crt.png` and keeps the clean PNG;
  on a -crt.png it restarts from the clean one. `gen crt PATH... [--lines N ...]` re-tunes finished images.
  `retro_digital` uses it. `[daily.props]` (category = [items]) + `props_per_day`: date-picked 80s items, one per
  category, added to the theme ("..., with X and Y in the scene"); run.json `daily.props`.
- `templates/contact_sheet.html`: the per-run `index.html` viewer (fit-all / size slider, A/B compare).
  Static, no build, opened via file://; `write_contact_sheet` inlines the run data as JSON.
- `mcp_server.py`: MCP tools (generate, get_job, vary, edit, cutout, doctor, list_presets, recent_runs, view_image, set_wallpaper,
  open_contact_sheet, project_* incl. project_rate/project_note, character_*)
- `projects.py`: creative projects (#22) behind `gen project new|list|status|set|gen|promote|keep` and the MCP
  `project_*` tools. `<output_dir>/projects/<slug>/project.json` holds the state (prompt, snippets, params,
  palette, refs), a version per change, named assets with history, and passes (tagged `gen` runs).
  Assets go stale when a later version changes prompt/negative/snippets/palette/refs.
  #46: projects also hold `docs` (.md/.txt, `project_doc`) and `cast` (shared characters); `gen project import`
  brings in a folder (images -> assets, docs; dry run unless --yes). Assets record `shows` {character: version}.
- `characters.py`: shared characters behind `gen char new|list|status|set` and the MCP `character_*` tools.
  `<output_dir>/characters/<name>/character.json` (description, palette, refs, versions). Passes put the cast's
  descriptions first; a character change stales the assets that show it in every project (unless pinned).
  `gen char new tulu --from-project tulu` moves a single-subject project into a character without staling.
- `boards.py`: the board (#7) is the project's triage view, not a separate object. `board.json` next to
  project.json holds pick/maybe/reject ratings and notes per image (key: path relative to output_dir; images
  are addressed as `P:N`, pass P image N) plus a log; rating never makes a project version. `projects.save()`
  rebuilds `<project>/index.html` from `templates/board.html` (passes grouped by version, History popup; buttons copy the
  `gen project rate|note` command). `gen project rate|note|board`, MCP `project_rate`/`project_note`;
  `project_get` returns `board` and the ratings/notes in `new_since_last_look`.
- `stages.py`: character stages (#68, v1; process in `docs/character-stages.md`): a character's `state.stage`
  (explore/converge/polish, `gen char set NAME --stage S`, stales nothing); a project works in its first cast
  character's stage. `run_stage` makes a pass the way `[stages.*]` in config.toml says (mode text/edit/img2img,
  preset, count, `instruction` with `{text}`, `ideas` = one per image via `generate(prompts=...)`, `needs_text`),
  editing the selected board image (`source` P:N/path/CHAR:REF) or the character's first ref; `projects.record_pass`
  records stage/source/text. The board gets a stage row (stage menu, one button per stage) and a "ref" button per card.
  Not built yet: poses, variants, base-version staleness for them (see #68).
- `boardserve.py`: `gen board serve [SLUG] [--port 8190] [--open]` (#55): the same board.html served on 127.0.0.1
  (stdlib http.server) with `"live": true`, so a click selects a card and buttons and keys (arrows, 1/2/3/0, n, o) rate and note via `/api/projects/<slug>/...`,
  "Generate 4 more" runs `projects.run_pass` (one job at a time), and the page polls `stamp` (project.json + board.json
  mtimes) to re-render on any change. Serves only images/.html under output_dir; POSTs must be same-origin JSON.
- `daily.py`: `gen daily` (#3): picks the day's theme from `[daily]` in config.toml (a date-seeded pick among themes
  not used by past daily runs in `no_repeat_days`; `[daily.dates]` ("MM-DD" or "YYYY-MM-DD"), `[daily.months]`, `[daily.weekdays]` replace them, most specific first), puts the `cast` characters' descriptions
  first, runs the preset, sets the first image as wallpaper. Default preset `wallpaper_cast` (#52, scene first: Qwen draws the theme
  with a stand-in per cast character (`stand_in`, "on the left"...), then a Qwen edit replaces each with the character from its ref
  (`[daily] ref`: one or a list, default each cast character's first ref; `scene_prompt`/`cast_prompt` templates, `cast_prompts()`).
  `wallpaper_ref` (edit-only, #51) still works: the first ref drawn into a scene at 16:9 + 4x upscale, text from `ref_prompt`. Failures leave the wallpaper alone; every attempt
  adds a line to `<output_dir>/daily.log`. `gen daily --show [--date D]` previews.
  `windows/daily-task.sh on [--at HH:MM]|off|status|run`: the Windows scheduled task (user-level, logged-on only,
  start-when-available) that runs it via `conhost --headless wsl.exe ... imagegen.py daily`.
- `progress.py`: live progress for `generate()` (#61): `describe` (the preset header line: models, passes, steps,
  sizes), and on a TTY (`IMAGEGEN_PROGRESS=0|1` overrides) `Progress`, a block redrawn in place from `Events.listeners`
  (stages done/running/pending, sampler step, ETA from the preset's past run.json timings, VRAM/RAM bars from
  /system_stats). `imagegen.log()` prints above it while `_LIVE` is set. Imports nothing from imagegen.
- `tunnel.py` (#71, a stopgap until project threads can use trusted devices): `gen tunnel`, started by hand on tulu,
  serves an allow-list of the MCP tools (`TOOLS`: render + read; image paths only inside output_dir, `LIMITS`) on
  127.0.0.1 behind a fresh bearer token and a cloudflared quick tunnel; logs every request, Ctrl-C/idle/20 bad
  requests close it. A thread with the URL + token from Þór's message runs `python tunnel.py tools|call TOOL '{json}'`
  (images saved, paths printed); without them, copy-paste commands as before. Never store the token. Threat model,
  CVE notes and removal: `docs/tunnel.md`. `tunnel-cloudflared.sh install|status|remove`: pinned, sha256-checked binary.
- `doctor.py`: read-only health checks behind `gen doctor` and the MCP `doctor` tool
  (warns when the limits are off while the wallpaper/daily presets use Qwen, `HEAVY_UNETS`)
- `comfy/`: opt-in resource limits for the comfyui service, **off by default**. `comfy/limits.sh on|off|status`
  installs/removes `/etc/systemd/system/comfyui.service.d/zz-imagegen-limits.conf` built from `comfyui-args`
  (extra CLI flags) + `limits.conf` (Nice/CPUWeight/IOWeight). on/off restarts comfyui (sudo) after a y/N prompt
  that warns if a job is running (`-y` skips it; no terminal = no).
  `update.sh` runs `comfy/install.sh` on comfy/ changes, which re-applies only if limits are already on.
  It applies steps from the last applied commit (`.git/imagegen-applied`), so a manual `git pull` is still caught; `--since REV` overrides.
- `windows/install-wslconfig.sh`: writes `%USERPROFILE%\.wslconfig` (cores/RAM for WSL). Manual, never overwrites,
  always-on once applied (needs `wsl --shutdown`), so it is not part of the on/off toggle.
- `models/*.sh`: opt-in downloads (ask y/N, resumable, sha256) into `~/comfyui/models`, sharing `models/_fetch.sh`:
  `flux-kontext.sh` (~17.7 GB), `flux2-klein.sh` (~11.9 GB), `dreamshaper-xl-lightning.sh` (~6.9 GB, SDXL finetune), `ipadapter-sdxl.sh` (~3.4 GB; the ComfyUI_IPAdapter_plus node is a manual git clone), `qwen-image-2.1.sh` (~30 GB, bf16: no int8, it renders
  black on gfx1201). `qwen3vl-8b-fp8.sh` (~10.6 GB): the fp8 text encoder for `qwen_lite` (bf16's 16 GB swapped WSL and stalled Windows). A preset's `download` key names its script; `gen doctor` shows those models (and nodes only
  they use) as warn while missing. Never run automatically.
- `test.sh` + `tests/`: the test suite (see Testing below). `requirements-dev.txt` / `requirements-browser.txt`
  hold test-only deps; CI runs it on every PR (`.github/workflows/test.yml`).
- `setup.sh`: idempotent setup (venv `~/venvs/imagegen`, `gen` wrapper, upscaler + rembg models)

## Rules
- Never `pip install` into `~/venvs/comfy` without excluding torch/torchvision/torchaudio/triton:
  it holds AMD's ROCm wheels and a generic torch would replace them.
- Models: `~/comfyui/models/{checkpoints,upscale_models,...}`.
- Adding a preset = new `[presets.x]` table in config.toml (+ a workflow JSON if the graph differs).
  Every `{{key}}` in the workflow must be a preset key, or one of: seed, cast_seed (a second sampler's seed, `cast_seed()`), prompt, negative, checkpoint,
  and for img2img/inpaint workflows also image, mask, denoise (width/height then come from the source).
  A preset picks those graphs with `img2img_workflow` / `inpaint_workflow` (defaults: sdxl_img2img.json, sdxl_inpaint.json).
  Post steps (`post = [...]`, see `POST` in imagegen.py) run on each output in order: `remove_bg`, then the
  optional `keep_largest` (drop stray shapes) and `trim` (crop + centre on a square of the shorter side); the last two rewrite the
  transparent PNG in place and skip images without transparency.
  An edit preset has `edit_workflow` (gets image, width/height; no checkpoint/negative); without `workflow` it is edit-only.
  A cast preset (#52, `wallpaper_cast`) has a `workflow` with one LoadImage `"{{ref}}"`: `generate(refs=[...], cast_prompt=)` /
  `gen --ref IMG --cast-prompt` / MCP `generate(refs=)`; `expand_refs` clones that node per ref and its `images.image_K` input
  (K, K+1, ...), `cast_prompt` (preset key, `{images}` = "image 2".."images 2 and 3") fills `{{cast_prompt}}`. A SaveImage with
  `filename_prefix = "imagegen-scene"` (`SCENE_PREFIX`) is a by-product: saved as `NN-seedS-scene.png` (sidecar/result `scene`),
  never in `images`, not checked for black. Refs are saved as `ref-N.png`; the sheet shows refs and scenes as input cards.
  `ref_canvas = [w, h]` fits the source onto a canvas of its corner colour first (Qwen edits draw at the source's
  aspect; any other latent size gave a blank image), used by `wallpaper_ref` for 16:9 from a portrait ref.
  A preset with `unet` (Flux/Qwen diffusion model) gets no default img2img/inpaint graph (`imagegen.mode_workflow`).
  steps/cfg/guidance/negative the chosen workflow has no `{{key}}` for are ignored with a note on stderr
  (`gen vary` on a klein/qwen image falls back to draft unless -p is given).
- Outputs: `<output_dir>/<YYYY-MM-DD>/<HHMMSS>-<preset>-<slug>/` with PNGs, per-image JSON sidecars
  (prompt, seed, params, timing), `run.json` and `index.html` contact sheet.
- Waiting (#51): a /history poll that times out means ComfyUI is busy (swapping), not gone: `wait_entry` waits up to `JOB_TIMEOUT`; only refused/reset connections give up after `DOWN_LIMIT` (90s).
  When a `generate` stops (error, timeout, Ctrl-C), `Comfy.cancel` deletes its queued jobs and interrupts the running one; `gen bakeoff` refuses to start while ComfyUI is busy (`Comfy.busy`).
  No websocket frame at all for `HANG_AFTER` (20 min, `Events.idle`) while a job is unfinished = ComfyUI's worker is dead (an HSA alloc failure did that): stop with the restart command.
- Blank images (#51): a job whose PNG is all black (`is_blank`: Qwen NaNs under memory pressure) is queued again
  with the same seed (`BLANK_RETRIES`), at the front of the queue and with an `imagegen_retry` input on the seed node
  (`_uncached`: ComfyUI ignores it when running but its cache would otherwise return the black image). One still black
  is kept with `"blank": true` (result + sidecar); `gen daily` and `gen --wallpaper` only set a non-blank image
  (all blank = fail, wallpaper unchanged). Fake: `comfy.blank_jobs = N` (the first N jobs queued), `comfy.front`.
- Timing (#2): `Events` in imagegen.py listens on ComfyUI's `/ws` (stdlib websocket client) and `job_timing`
  turns the `executing` events into `<stage>_s` seconds (stages from the node class, `STAGES`). Without the
  socket it falls back to `/history` timestamps (queue_s and comfy_s only). `run.json` `timing.summary` is the
  one-line breakdown that `gen`, the MCP result and the contact sheet show.
- Logs: `journalctl -u comfyui -f`.

## Testing
Run `./test.sh` before every push (any args go to pytest, e.g. `./test.sh -k projects -x`). It needs no tulu,
GPU or ComfyUI: the first run makes `.venv-test/` from requirements-dev.txt, and every test is isolated
(temp output_dir, IMAGEGEN_COMFY_URL on a closed port, rembg stubbed, `tests/stubs/` first on PATH).
Cloud sessions also run the contact sheet in Chromium (`/opt/pw-browsers`); elsewhere add `--browser`.

What's there to reuse, instead of building a one-off setup:
- `comfy` fixture (`tests/fake_comfy.py`): a fake ComfyUI HTTP server with canned PNGs. Inspect `comfy.workflows()`,
  `comfy.uploads`; switch on `reject`, `fail_jobs`, `no_gpu`, `delay` (slow jobs), `queue_running/pending`, `running_ids`, `blank_jobs`;
  cancels land in `deleted`, `cleared`, `interrupts`.
  It also plays the websocket events per node: `node_s` (seconds per class_type), `cached`, `ws = False`.
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
