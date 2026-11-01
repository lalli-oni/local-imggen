#!/usr/bin/env python3
"""MCP server exposing imagegen to Claude. Launched by the Claude desktop app via wsl.exe (stdio)."""
from __future__ import annotations

import io
import threading
import time
import uuid

from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PILImage

import imagegen

mcp = MCPServer(
    "imagegen",
    instructions=(
        "Local image generation on the user's GPU (ComfyUI + SDXL; FLUX.1 Kontext for instruction edits via "
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


@mcp.tool()
def list_presets() -> dict:
    """List available presets with descriptions and generation sizes."""
    out = {}
    for name, p in imagegen.list_presets().items():
        if "workflow" not in p:  # edit-only (e.g. kontext): use the edit tool
            out[name] = {"description": p.get("description", ""), "mode": "edit only", "size": "~1 MP, source aspect"}
            continue
        out[name] = {"description": p.get("description", ""), "size": f"{p['width']}x{p['height']}",
                     **({"output_size": f"{p['target_width']}x{p['target_height']}"} if "target_width" in p else {})}
    return out


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
               "images": [{"path": im["path"], "windows_path": im["windows_path"], "seed": im["seed"]}
                          for im in run["images"]]}
    out: list = [summary]
    if job["preview"]:
        out += [_thumb(im["path"]) for im in run["images"]]
    return out


@mcp.tool()
def generate(prompt: str, preset: str = "draft", count: int = 1, seed: int | None = None,
             negative: str | None = None, width: int | None = None, height: int | None = None,
             steps: int | None = None, preview: bool = True) -> list:
    """Generate images with a preset. count 1-8 uses consecutive seeds (seed, seed+1, ...).

    Waits up to ~45s. If finished: returns run info (paths, contact sheet) plus JPEG thumbnails
    (if preview=True). If still running: returns {status: "running", job_id} -- call get_job(job_id)
    until done. Rough timings: draft ~15-25s/image, icon ~25s, wallpaper ~70s/image.
    Re-use a seed to reproduce or refine an image.
    """
    kwargs = dict(prompt=prompt, preset=preset, count=min(max(1, count), MAX_COUNT), seed=seed,
                  negative=negative, width=width, height=height, steps=steps)
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
         seed: int | None = None, preview: bool = True) -> list:
    """Variations of an existing image (img2img), or inpaint with a mask. path/mask: Linux or Windows paths.

    strength = how much may change, 0-1 (default 0.4; 0.85 with a mask). ~0.25 keeps the composition,
    ~0.6+ reinterprets it. mask: white = repaint, black = keep; pixels away from the (slightly feathered)
    mask edge are kept. Output is ~1 MP (source aspect), except wallpaper presets which upscale.
    prompt/preset/negative default to the image's sidecar JSON; an outside image needs a prompt.
    Same waiting/job_id behaviour as generate.
    """
    kwargs = dict(path=path, count=min(max(1, count), MAX_COUNT), strength=strength, prompt=prompt,
                  negative=negative, preset=preset, mask=mask, seed=seed)
    return _start(imagegen.vary, kwargs, preview)


MAX_EDITS = 4


@mcp.tool()
def edit(path: str, instruction: str, count: int = 1, seed: int | None = None, steps: int | None = None,
         guidance: float | None = None, preview: bool = True) -> list:
    """Edit an image by plain-language instruction with FLUX.1 Kontext. path: Linux or Windows path.

    Write the instruction as what to change plus what to keep, e.g. "Make the character wave with one
    tentacle, seen from the side. Keep the same character design, colors, proportions and art style."
    Name things concretely ("the small green octopus creature") rather than "it". One change per edit
    works best; chain edits by editing a result. count 1-4: each is a fresh edit of the source with
    its own seed. guidance (default 2.5): higher follows the text harder. Output is ~1 MP.
    Slow: ~75-85s per image on tulu once the model is loaded, ~3 min for the first image of a session
    (loading ~17 GB). Same waiting/job_id behaviour as generate, so poll get_job.
    Needs the opt-in Kontext models (doctor shows them as warn if missing).
    """
    kwargs = dict(path=path, instruction=instruction, count=min(max(1, count), MAX_EDITS), seed=seed,
                  steps=steps, guidance=guidance)
    return _start(imagegen.edit, kwargs, preview)


@mcp.tool()
def cutout(path: str) -> dict:
    """Remove an image's background (rembg) -> <name>-transparent.png next to it."""
    out = imagegen.remove_bg(imagegen.to_linux_path(path))
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
def view_image(path: str, size: int = 768) -> Image:
    """Return a preview of an existing image (Linux or Windows path), e.g. from recent_runs."""
    return _thumb(str(imagegen.to_linux_path(path)), min(size, 1536))


@mcp.tool()
def set_wallpaper(path: str) -> str:
    """Set an image (Linux or Windows path) as the Windows desktop wallpaper."""
    return f"wallpaper set: {imagegen.set_wallpaper(path)}"


@mcp.tool()
def open_contact_sheet(path: str) -> str:
    """Open a run's index.html contact sheet in the user's Windows browser."""
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
    """A project's current state (prompt, snippets, params, palette, refs), its assets with history and
    stale flags, recent versions and passes, and new_since_last_look: what changed since your previous
    project_get. preview=True adds thumbnails of the refs and current asset versions.
    Use this to summarise a project or to decide the next pass."""
    import projects
    s = projects.summary(slug, mark_seen="claude", history=history)
    out: list = [s]
    if preview:
        out += [_thumb(r["path"]) for r in s["state"]["refs"].values()]
        out += [_thumb(a["file"]) for a in s["assets"].values()]
    return out


@mcp.tool()
def project_new(slug: str, title: str | None = None, brief: str = "", prompt: str | None = None,
                preset: str | None = None, palette: list[str] | None = None) -> dict:
    """Create a project (v1). slug: lowercase letters, digits, dashes. Refine it with project_update."""
    import projects
    kw = {k: v for k, v in dict(prompt=prompt, preset=preset, palette=palette).items() if v is not None}
    projects.new(slug, title, brief, "claude", **kw)
    return projects.summary(slug, mark_seen="claude")


@mcp.tool()
def project_update(slug: str, note: str, prompt: str | None = None, negative: str | None = None,
                   preset: str | None = None, params: dict | None = None, snippets: dict | None = None,
                   palette: list[str] | None = None, refs: dict | None = None,
                   drop_refs: list[str] | None = None) -> dict:
    """Change a project's state; creates a new project version with your note (say why).
    params: {width, height, steps, cfg, strength}; snippets: {name: text}, appended to the prompt in order;
    a null value removes a param or snippet. palette: hex list (replaces it). refs: {name: image path}.
    Changing prompt, negative, snippets, palette or refs makes existing assets stale."""
    import projects
    kw = {k: v for k, v in dict(prompt=prompt, negative=negative, preset=preset, params=params,
                                snippets=snippets, palette=palette, refs=refs,
                                drop_refs=drop_refs).items() if v is not None}
    return projects.set_state(slug, note, "claude", **kw)


@mcp.tool()
def project_generate(slug: str, count: int = 4, extra: str | None = None, seed: int | None = None,
                     ref: str | None = None, strength: float | None = None, preview: bool = True) -> list:
    """A pass: generate from the project's current state (prompt + snippets, preset, params).
    extra is appended to the prompt for this pass only. ref = img2img from one of the project's refs
    (strength defaults to the project's strength param, else 0.4). The images record the project version.
    Same waiting/job_id behaviour as generate."""
    import projects
    kwargs = dict(slug=slug, count=min(max(1, count), MAX_COUNT), extra=extra, seed=seed, ref=ref,
                  strength=strength, author="claude")
    return _start(projects.run_pass, kwargs, preview)


@mcp.tool()
def project_promote(slug: str, image: str, asset: str, note: str, as_ref: str | None = None) -> dict:
    """Make an image the next version of a named asset (e.g. "tulu", "icon/folder"); creates a project
    version. as_ref also makes it a project reference image. Only promote what the user picked."""
    import projects
    return projects.promote(slug, image, asset, note, as_ref, "claude")


@mcp.tool()
def project_keep(slug: str, asset: str, note: str) -> dict:
    """Mark a stale asset as fine as-is at the current project version (only when the user says so)."""
    import projects
    return projects.keep(slug, asset, note, "claude")


if __name__ == "__main__":
    mcp.run("stdio")
