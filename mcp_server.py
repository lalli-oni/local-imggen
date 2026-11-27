#!/usr/bin/env python3
"""MCP server exposing imagegen to Claude. Launched by the Claude desktop app via wsl.exe (stdio)."""
from __future__ import annotations

import io
import os
import threading
import time
import uuid

from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PILImage

import imagegen

mcp = MCPServer(
    "imagegen",
    instructions=(
        "Local image generation on the user's GPU (ComfyUI + SDXL; FLUX.2 klein for instruction edits via "
        "the edit tool). Call list_presets first if unsure. "
        "Outputs land in the user's Windows Pictures\\imagegen folder; each run has an index.html contact sheet. "
        "SDXL prompts work best as comma-separated descriptive phrases; presets append their own style suffix."
    ),
)

MAX_COUNT = 8


def _thumb(path: str, size: int = 384) -> Image:
    im = PILImage.open(path)
    im.thumbnail((size, size))
    if im.mode in ("RGBA", "LA"):  # flatten transparency onto grey so icons stay visible
        bg = PILImage.new("RGB", im.size, (128, 128, 128))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=80)
    return Image(data=buf.getvalue(), format="jpeg")


def _target_size(p: dict) -> str | None:
    """'WxH' when the preset sets both target sizes; None otherwise (a preset with only one of them is not an error here)."""
    if "target_width" in p and "target_height" in p:
        return f"{p['target_width']}x{p['target_height']}"
    return None


@mcp.tool()
def list_presets() -> dict:
    """List available presets with descriptions and generation sizes."""
    out = {}
    for name, p in imagegen.list_presets().items():
        if "workflow" not in p:  # edit-only (e.g. kontext): use the edit tool
            target = _target_size(p)
            size = f"{target} (upscaled)" if target else "~1 MP, source aspect"
            out[name] = {"description": p.get("description", ""), "mode": "edit only", "size": size,
                         **({"styles": p["styles"]} if p.get("styles") else {})}
            continue
        target = _target_size(p)
        out[name] = {"description": p.get("description", ""), "size": f"{p['width']}x{p['height']}",
                     **({"output_size": target} if target else {}),
                     **({"styles": p["styles"]} if p.get("styles") else {})}
    return out


@mcp.tool()
def list_styles() -> dict:
    """Looks ([styles.NAME] in config.toml) for generate(styles=[...]) or a project snippet "@NAME":
    {name: {description, text}}. A preset's own default styles are in list_presets."""
    return {name: {"description": st.get("description", ""), "text": imagegen.style_text([name])}
            for name, st in imagegen.list_styles().items()}


SEEN_AS = "claude"  # the reader project_get marks as having seen (gen tunnel serves as "thread", #71)
WAIT_S = 45  # the remote bridge times out at ~60s, so never block longer than this per call
JOBS: dict[str, dict] = {}


def _run_job(job_id: str, kwargs: dict, fn=imagegen.generate) -> None:
    job = JOBS[job_id]
    try:
        job["result"] = fn(**kwargs)
        job["status"] = "done"
    except Exception as e:  # surfaced via get_job
        job["error"] = f"{type(e).__name__}: {e}"
        job["status"] = "error"


def _wait_and_report(job_id: str, wait: float) -> list:
    job = JOBS.get(job_id)
    if job is None:
        raise ValueError(f"unknown job '{job_id}' (jobs are lost if the MCP server restarts; check recent_runs)")
    job["thread"].join(timeout=max(0.0, min(wait, WAIT_S)))
    if job["status"] == "running":
        return [{"job_id": job_id, "status": "running",
                 "elapsed_s": round(time.time() - job["started"]),
                 "next": f"call get_job(job_id='{job_id}') to keep waiting"}]
    if job["status"] == "error":
        return [{"job_id": job_id, "status": "error", "error": job["error"]}]
    run = job["result"]
    summary = {"job_id": job_id, "status": "done", "elapsed_s": round(time.time() - job["started"]),
               "run_dir_windows": run["run_dir_windows"], "contact_sheet_windows": run["contact_sheet_windows"],
               "base_seed": run["base_seed"], "timing": (run.get("timing") or {}).get("summary"),
               "images": [{"path": im["path"], "windows_path": im.get("windows_path"), "seed": im["seed"]}
                          for im in run["images"]]}
    out: list = [summary]
    if job["preview"]:
        out += [_thumb(im["path"]) for im in run["images"]]
    return out


@mcp.tool()
def generate(prompt: str, preset: str = "draft", count: int = 1, seed: int | None = None,
             negative: str | None = None, width: int | None = None, height: int | None = None,
             steps: int | None = None, preview: bool = True, styles: list[str] | None = None,
             refs: list[str] | None = None, cast_prompt: str | None = None) -> list:
    """Generate images with a preset. count 1-8 uses consecutive seeds (seed, seed+1, ...).

    styles: looks from list_styles() added to the prompt (default: the preset's own, its "styles" in
    list_presets, e.g. synthwave on the wallpaper presets; [] = none).
    refs / cast_prompt: for wallpaper_cast (scene first, then cast): the prompt describes the scene, refs are
    character ref images (Linux or Windows paths) and cast_prompt the edit that puts them in ("image 1" = the
    scene, "image 2".. = the refs; default the preset's). ~6 min an image; the scene is kept as *-scene.png.

    Waits up to ~45s. If finished: returns run info (paths, contact sheet) plus JPEG thumbnails
    (if preview=True). If still running: returns {status: "running", job_id} -- call get_job(job_id)
    until done. Rough timings: draft ~15-25s/image, icon ~25s, wallpaper ~70s/image.
    Re-use a seed to reproduce or refine an image.
    """
    kwargs = dict(prompt=prompt, preset=preset, count=min(max(1, count), MAX_COUNT), seed=seed,
                  negative=negative, width=width, height=height, steps=steps, styles=styles,
                  refs=refs, cast_prompt=cast_prompt)
    return _start(imagegen.generate, kwargs, preview)


def _start(fn, kwargs: dict, preview: bool) -> list:
    job_id = uuid.uuid4().hex[:8]
    t = threading.Thread(target=_run_job, args=(job_id, kwargs, fn), daemon=True)
    JOBS[job_id] = {"status": "running", "thread": t, "started": time.time(), "preview": preview}
    t.start()
    return _wait_and_report(job_id, WAIT_S)


@mcp.tool()
def vary(path: str, count: int = 4, strength: float | None = None, prompt: str | None = None,
         negative: str | None = None, preset: str | None = None, mask: str | None = None,
         seed: int | None = None, preview: bool = True, styles: list[str] | None = None) -> list:
    """Variations of an existing image (img2img), or inpaint with a mask. path/mask: Linux or Windows paths.

    strength = how much may change, 0-1 (default 0.4; 0.85 with a mask). ~0.25 keeps the composition,
    ~0.6+ reinterprets it. mask: white = repaint, black = keep; pixels away from the (slightly feathered)
    mask edge are kept. Output is ~1 MP (source aspect), except wallpaper presets which upscale.
    prompt/preset/negative/styles default to the image's sidecar JSON; an outside image needs a prompt.
    styles: list_styles() looks ([] = none).
    Same waiting/job_id behaviour as generate.
    """
    kwargs = dict(path=path, count=min(max(1, count), MAX_COUNT), strength=strength, prompt=prompt,
                  negative=negative, preset=preset, mask=mask, seed=seed, styles=styles)
    return _start(imagegen.vary, kwargs, preview)


MAX_EDITS = 4


@mcp.tool()
def edit(path: str, instruction: str, count: int = 1, seed: int | None = None, steps: int | None = None,
         guidance: float | None = None, preview: bool = True, preset: str = imagegen.EDIT_PRESET,
         styles: list[str] | None = None, refs: list[str] | None = None) -> list:
    """Edit an image by plain-language instruction (default FLUX.2 klein). path: Linux or Windows path.

    Write the instruction as what to change plus what to keep, e.g. "Make the character wave with one
    tentacle, seen from the side. Keep the same character design, colors, proportions and art style."
    Name things concretely ("the small green octopus creature") rather than "it". One change per edit
    works best; chain edits by editing a result. count 1-4: each is a fresh edit of the source with
    its own seed. Output is ~1 MP.
    preset: "klein" (default; ~7-16s per image on tulu once loaded, ~1 min for the first),
    "kontext" (FLUX.1 Kontext, ~2 min per image; guidance, default 2.5, only applies here) or
    "qwen" (Qwen-Image-2.1, follows text most literally but 2-9 min per image).
    Same waiting/job_id behaviour as generate, so poll get_job.
    Needs the preset's opt-in models (doctor shows them as warn if missing).
    styles: list_styles() looks added to the instruction (default: the preset's own, e.g. synthwave on
    wallpaper_ref; [] = none).
    refs: with preset "wallpaper_cast", redo only the cast pass on a saved scene (NN-seedS-scene.png): ref
    images of the characters, as image 2, 3...; e.g. "Replace the main character in image 1 with the character
    from image 2." ({images} = "image 2" / "images 2 and 3").
    """
    kwargs = dict(path=path, instruction=instruction, count=min(max(1, count), MAX_EDITS), seed=seed,
                  steps=steps, guidance=guidance, preset=preset, styles=styles, refs=refs)
    return _start(imagegen.edit, kwargs, preview)


@mcp.tool()
def sweep(prompt: str, presets: list[str] | None = None, steps: list[int] | dict[str, list[int]] | None = None,
          cfg: list[float] | None = None, guidance: list[float] | None = None, styles: list[str] | None = None,
          seeds: int = 1, seed: int | None = None, width: int | None = None, height: int | None = None,
          rows: str | None = None, confirm: bool = False, preview: bool = False) -> list:
    """Compare settings or models: one prompt over a grid, as a labelled contact sheet (#17).

    Every argument that gets more than one value is an axis; the images are the product (same prompt and the same
    seeds in every cell). presets = models to compare (e.g. ["klein", "qwen"], default ["draft"]), steps (a list for every model, or {"klein": [4, 8], "qwen": [15, 25]} per model, #94), cfg
    (SDXL/Qwen), guidance (no text-to-image preset takes it yet), styles (list_styles names, "none", "a+b"; default each preset's own look, give
    ["none"] plus one more to compare, or one value "none" to give every model the bare prompt), seeds = how many seeds
    (base, base+1, ...; rows share one). A setting a model has no input for (klein: cfg) is rendered once and the other
    values are skipped. width and height together fix the size of every cell. rows: the axis down the side (default
    seed if it varies, else the first of model, style, steps, cfg, guidance).
    More than 16 images needs confirm=True (it can take a long time: Qwen is minutes an image).
    Returns the run with the grid page (contact_sheet_windows); open_contact_sheet shows it. Same
    waiting/job_id behaviour as generate: poll get_job. Per-cell time and peak VRAM/RAM are in bakeoff.json.
    """
    if (width is None) != (height is None):
        raise ValueError("give width and height together")
    import sweep as sweep_mod
    kwargs = dict(prompt=prompt, presets=presets or ["draft"], steps=steps, cfg=cfg, guidance=guidance,
                  styles=styles, seeds=seeds, seed=seed, size=(width, height) if width else None, rows=rows,
                  confirm=confirm)
    return _start(sweep_mod.sweep, kwargs, preview)


@mcp.tool()
def cutout(path: str, clean: bool = False) -> dict:
    """Remove an image's background (rembg) -> <name>-transparent.png next to it.
    clean=True also drops stray shapes left around the subject and centres it on a square canvas
    (what the icon preset does). Leave it off for scenes or several separate subjects."""
    out = imagegen.cutout(imagegen.to_linux_path(path), clean)
    return {"path": str(out), "windows_path": imagegen.to_windows_path(out)}


@mcp.tool()
def get_job(job_id: str, wait: int = 45) -> list:
    """Wait (up to 45s) for a generate job and return its result, or {status: "running"} again."""
    return _wait_and_report(job_id, wait)


@mcp.tool()
def recent_runs(n: int = 5) -> list[dict]:
    """List the most recent generation runs (newest first) with their image paths."""
    return imagegen.recent_runs(n)


@mcp.tool()
def run_info(run_dir: str) -> dict:
    """A generation run's run.json: params, timings and VRAM/RAM (timing.summary), seeds, sweep cells.
    run_dir is a run folder: run_dir from recent_runs, or run_dir_windows from get_job (or the run.json inside it)."""
    return imagegen.read_run(run_dir)


@mcp.tool()
def view_image(path: str, size: int = 768) -> Image:
    """Return a preview of an existing image (Linux or Windows path), e.g. from recent_runs."""
    return _thumb(str(imagegen.to_linux_path(path)), min(size, 1536))


@mcp.tool()
def set_wallpaper(path: str) -> str:
    """Set an image (Linux or Windows path) as the Windows desktop wallpaper."""
    return f"wallpaper set: {imagegen.set_wallpaper(path)}"


@mcp.tool()
def open_contact_sheet(path: str) -> str:
    """Open a run's index.html contact sheet, or a project's board page (project_get board.page),
    in the user's Windows browser."""
    imagegen.open_in_windows(str(imagegen.to_linux_path(path)))
    return "opened"


@mcp.tool()
def doctor() -> list[dict]:
    """Health check: comfyui service, GPU, ROCm torch, models, presets, output_dir, limits.
    Each item has status ok|warn|fail|info and, for problems, a fix hint. Read-only."""
    import doctor as _doctor
    return _doctor.run_checks(include_mcp=False)


# ---------------------------------------------------------------- creative projects (projects.py)

@mcp.tool()
def project_list() -> list[dict]:
    """List creative projects (slug, title, current version)."""
    import projects
    return [{"slug": s, "title": (p := projects.load(s))["title"], "version": p["version"]}
            for s in projects.list_projects()]


@mcp.tool()
def project_get(slug: str, preview: bool = False, history: int = 5) -> list:
    """A project's current state (prompt, snippets, params, palette, refs, cast characters, docs), its
    assets with history, the characters they show and stale flags, recent versions and passes, and
    new_since_last_look: what changed since your previous project_get (versions, passes, and the
    ratings/notes on the board). board: rating counts, every rated or noted image (picks first, with
    its pass id "P:N" and project version) and the board notes; board.page is the user's board page.
    stage (#68): the first cast character's stage and what each stage's generate runs
    (project_generate stage=...; docs/character-stages.md).
    preview=True adds thumbnails of the refs, current asset versions and up to 8 picks.
    Read docs with project_doc. Use this to summarise a project or to decide the next pass: base the
    next prompt version on what the user picked and rejected, and on their notes."""
    import projects
    s = projects.summary(slug, mark_seen=SEEN_AS, history=history)
    out: list = [s]
    if preview:
        out += [_thumb(r["path"]) for r in s["state"]["refs"].values()]
        out += [_thumb(a["file"]) for a in s["assets"].values()]
        picks = [im["image"] for im in s["board"]["images"] if im.get("rating") == "pick"]
        out += [_thumb(p) for p in [p for p in picks if os.path.isfile(p)][:8]]  # a pick's run may be gone
    return out


@mcp.tool()
def project_new(slug: str, title: str | None = None, brief: str = "", prompt: str | None = None,
                preset: str | None = None, palette: list[str] | None = None) -> dict:
    """Create a project (v1). slug: lowercase letters, digits, dashes. Refine it with project_update."""
    import projects
    kw = {k: v for k, v in dict(prompt=prompt, preset=preset, palette=palette).items() if v is not None}
    projects.new(slug, title, brief, "claude", **kw)
    return projects.summary(slug, mark_seen=SEEN_AS)


@mcp.tool()
def project_update(slug: str, note: str, prompt: str | None = None, negative: str | None = None,
                   preset: str | None = None, params: dict | None = None, snippets: dict | None = None,
                   palette: list[str] | None = None, refs: dict | None = None,
                   drop_refs: list[str] | None = None, cast: dict | None = None,
                   uncast: list[str] | None = None, docs: dict | None = None) -> dict:
    """Change a project's state; creates a new project version with your note (say why).
    params: {width, height, steps, cfg, strength}; snippets: {name: text}, appended to the prompt in order
    (text "@STYLE" copies the prompt text of a list_styles() look, not its negative);
    a null value removes a param or snippet. palette: hex list (replaces it). refs: {name: image path}.
    cast: {character: null, or a version to pin}: shared characters (character_*) whose descriptions go
    in front of the prompt. docs: {name: path to .md/.txt} (null removes).
    Changing prompt, negative, snippets, palette or refs makes existing assets stale."""
    import projects
    kw = {k: v for k, v in dict(prompt=prompt, negative=negative, preset=preset, params=params,
                                snippets=snippets, palette=palette, refs=refs,
                                drop_refs=drop_refs, cast=cast, uncast=uncast,
                                docs=docs).items() if v is not None}
    return projects.set_state(slug, note, "claude", **kw)


@mcp.tool()
def project_generate(slug: str, count: int | None = None, extra: str | None = None, seed: int | None = None,
                     ref: str | None = None, strength: float | None = None, preview: bool = True,
                     chars: list[str] | None = None, stage: str | None = None, source: str | None = None,
                     text: str | None = None) -> list:
    """A pass: generate from the project's current state (cast descriptions + prompt + snippets,
    preset, params). chars: which cast characters to draw (default all, [] = none).
    extra is appended to the prompt for this pass only. ref = img2img from one of the project's refs,
    or "character:ref" for a cast character's ref (strength defaults to the project's strength param,
    else 0.4). The images record the project and character versions.
    stage ("explore" / "converge" / "polish", or "current" for the character's stage; see
    docs/character-stages.md): generate the way that stage does instead, with its preset and count
    (count: default the stage's); source = the image converge/polish edit ("P:N",
    a path or "character:ref"; default the character's first ref); text = explore: extra prompt,
    converge: one idea for every image (default: the stage's idea list), polish: the fix (required).
    Same waiting/job_id behaviour as generate."""
    import projects
    if stage is not None:
        import stages
        if extra or ref or strength is not None or chars is not None:
            raise ValueError("a stage pass takes source, text, count and seed (the stage settings decide the rest)")
        name = None if stage == "current" else stage
        if count is None:  # the stage's count, within the same limit as any other pass
            cur = stages.of_project(projects.load(slug)["state"])
            count = int(stages.settings(name or (cur or {}).get("stage") or "explore")["count"])
        kwargs = dict(slug=slug, stage=name, count=min(max(1, count), MAX_COUNT),
                      source=source, text=text, seed=seed, author="claude")
        return _start(stages.run_stage, kwargs, preview)
    if source or text:
        raise ValueError("source and text need a stage")
    kwargs = dict(slug=slug, count=min(max(1, 4 if count is None else count), MAX_COUNT), extra=extra, seed=seed, ref=ref,
                  strength=strength, author="claude", chars=chars)
    return _start(projects.run_pass, kwargs, preview)


@mcp.tool()
def project_promote(slug: str, image: str, asset: str, note: str, as_ref: str | None = None,
                    shows: list[str] | None = None) -> dict:
    """Make an image the next version of a named asset (e.g. "tulu", "icon/folder"); creates a project
    version. as_ref also makes it a project reference image. shows: the characters in the image
    (default: the ones its pass drew); the asset goes stale when one of them changes.
    Only promote what the user picked."""
    import projects
    return projects.promote(slug, image, asset, note, as_ref, "claude", shows)


@mcp.tool()
def project_keep(slug: str, asset: str, note: str) -> dict:
    """Mark a stale asset as fine as-is at the current project version (only when the user says so)."""
    import projects
    return projects.keep(slug, asset, note, "claude")


@mcp.tool()
def project_rate(slug: str, images: list[str], rating: str, note: str | None = None) -> list[dict]:
    """Rate images on the project's board: "pick", "maybe", "reject", or "clear" to remove a rating.
    images: pass ids "P:N" (pass P, image N, as project_get lists them) or image paths.
    note (optional) is added to each image: say what you see. Rate as the user asked; when giving your
    own opinion, prefer project_note so the user's picks stay theirs. Does not make a project version."""
    import boards
    return boards.rate(slug, images, rating, note, "claude")


@mcp.tool()
def project_note(slug: str, text: str, image: str | None = None) -> dict:
    """Leave a note on the project's board, or on one image ("P:N" or a path): observations, why the
    next prompt version changes what it does. The user sees it on the board page."""
    import boards
    return boards.note(slug, text, image, "claude")


@mcp.tool()
def project_doc(slug: str, name: str) -> str:
    """The text of a project doc (character notes, lore, prompt guidelines). project_get lists them.
    Read the relevant ones before writing prompts for the project."""
    import projects
    return projects.read_doc(slug, name)


@mcp.tool()
def project_import(slug: str, folder: str, apply: bool = False, shows: list[str] | None = None,
                   title: str | None = None) -> dict:
    """Bring an existing folder into a project (creates it if new): images become assets (named after
    the file, subfolders become asset/name), .md/.txt files become docs. Windows paths work.
    apply=False (default) only returns the plan: show it to the user and apply only when they agree.
    shows: characters every imported image shows."""
    import projects
    return projects.import_folder(slug, folder, apply, shows, title, "claude")


# ---------------------------------------------------------------- shared characters (characters.py)

@mcp.tool()
def character_list() -> list[dict]:
    """List shared characters (name, title, version). Projects cast them (project_update cast=...)."""
    import characters
    return [{"name": n, "title": (c := characters.load(n))["title"], "version": c["version"]}
            for n in characters.list_characters()]


@mcp.tool()
def character_get(name: str, preview: bool = False) -> list:
    """A character's description, palette, refs, history, and which projects cast it (with the assets
    that show it). preview=True adds thumbnails of its refs."""
    import characters
    s = characters.summary(name)
    out: list = [s]
    if preview:
        out += [_thumb(r["path"]) for r in s["state"]["refs"].values()]
    return out


@mcp.tool()
def character_new(name: str, description: str, title: str | None = None,
                  palette: list[str] | None = None, refs: dict | None = None) -> dict:
    """Create a shared character (v1). description: the prompt text that draws it (traits, colours,
    style); refs: {name: image path}. Cast it in a project with project_update(cast={name: None})."""
    import characters
    kw = {k: v for k, v in dict(description=description, palette=palette, refs=refs).items() if v is not None}
    characters.new(name, title, "claude", **kw)
    return characters.summary(name)


@mcp.tool()
def character_update(name: str, note: str, description: str | None = None,
                     palette: list[str] | None = None, refs: dict | None = None,
                     drop_refs: list[str] | None = None, stage: str | None = None) -> dict:
    """Change a character; creates a new character version with your note. Every asset that shows it,
    in every project that casts it (unless pinned), goes stale. Only on the user's request.
    stage: explore / converge / polish (docs/character-stages.md); a stage change alone stales nothing."""
    import characters
    kw = {k: v for k, v in dict(description=description, palette=palette, refs=refs,
                                drop_refs=drop_refs, stage=stage).items() if v is not None}
    return characters.set_state(name, note, "claude", **kw)


if __name__ == "__main__":
    mcp.run("stdio")
