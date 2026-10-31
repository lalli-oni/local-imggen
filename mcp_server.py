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
        "Local image generation on the user's GPU (ComfyUI + SDXL). Call list_presets first if unsure. "
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
    return {name: {"description": p.get("description", ""), "size": f"{p['width']}x{p['height']}",
                   **({"output_size": f"{p['target_width']}x{p['target_height']}"} if "target_width" in p else {})}
            for name, p in imagegen.list_presets().items()}


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
               "base_seed": run["base_seed"],
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
    ~0.6+ reinterprets it. mask: white = repaint, black = keep; unmasked pixels stay exact.
    prompt/preset/negative default to the image's sidecar JSON; an outside image needs a prompt.
    Same waiting/job_id behaviour as generate.
    """
    kwargs = dict(path=path, count=min(max(1, count), MAX_COUNT), strength=strength, prompt=prompt,
                  negative=negative, preset=preset, mask=mask, seed=seed)
    return _start(imagegen.vary, kwargs, preview)


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


if __name__ == "__main__":
    mcp.run("stdio")
