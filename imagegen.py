#!/usr/bin/env python3
"""imagegen: run ComfyUI presets from the command line (and from mcp_server.py).

Usage:
  gen "a cozy cabin at dusk"                    # draft preset, 1 image
  gen "misty fjord" -p wallpaper -n 4           # 4 wallpapers (seeds base..base+3)
  gen "rocket" -p icon -n 6 --open              # icons, open contact sheet in browser
  gen "x" --seed 123 --size 832x1216 --steps 40 --neg "people"
  gen vary PATH -n 4 --strength 0.4             # img2img variations of an image
  gen vary PATH --mask MASK.png --prompt "..."  # inpaint: repaint only the white part of MASK
  gen cutout PATH                               # remove the background -> PATH-transparent.png
  gen presets                                   # list presets
  gen recent [N]                                # last N runs
  gen set-wallpaper PATH                        # set Windows desktop wallpaper
  gen doctor                                    # health check (service, GPU, torch, models, ...)
  gen sheet [RUN_DIR|DATE]                      # rebuild contact sheet(s); no arg = latest run
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_SEED = 2**32


def log(msg: str) -> None:
    # stderr only: stdout is the MCP protocol channel when used by mcp_server.py
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    if v := os.environ.get("IMAGEGEN_OUTPUT_DIR"):
        cfg["output_dir"] = v
    if v := os.environ.get("IMAGEGEN_COMFY_URL"):
        cfg["comfy_url"] = v
    return cfg


def list_presets() -> dict[str, dict]:
    return load_config()["presets"]


# ---------------------------------------------------------------- workflow templating

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def fill(node, params: dict):
    """Replace string values that are exactly "{{key}}" with params[key] (keeps type)."""
    if isinstance(node, dict):
        return {k: fill(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [fill(v, params) for v in node]
    if isinstance(node, str) and (m := _PLACEHOLDER.fullmatch(node)):
        key = m.group(1)
        if key not in params:
            raise KeyError(f"workflow placeholder '{{{{{key}}}}}' has no value (add it to the preset)")
        return params[key]
    return node


# ---------------------------------------------------------------- ComfyUI client

class Comfy:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.client_id = uuid.uuid4().hex

    def _get(self, path: str, timeout: float = 30) -> bytes:
        with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
            return r.read()

    def wait_ready(self, timeout: float = 120) -> None:
        """ComfyUI may still be booting (e.g. WSL just started). Poll until it answers."""
        t0 = time.time()
        while True:
            try:
                self._get("/system_stats", timeout=5)
                return
            except Exception:
                if time.time() - t0 > timeout:
                    raise RuntimeError(f"ComfyUI not reachable at {self.url} "
                                       "(is the service running? `systemctl status comfyui`)")
                time.sleep(2)

    def queue(self, workflow: dict) -> str:
        body = json.dumps({"prompt": workflow, "client_id": self.client_id}).encode()
        req = urllib.request.Request(self.url + "/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())["prompt_id"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ComfyUI rejected the workflow: {e.read().decode(errors='replace')[:2000]}")

    def wait(self, prompt_id: str, timeout: float = 1800) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            hist = json.loads(self._get(f"/history/{prompt_id}"))
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI failed: {json.dumps(status.get('messages'))[:2000]}")
                return entry.get("outputs", {})
            time.sleep(1)
        raise TimeoutError(f"prompt {prompt_id} did not finish in {timeout}s")

    def upload(self, data: bytes, name: str) -> str:
        """Upload a PNG to ComfyUI's input folder; returns the name LoadImage expects."""
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + data + \
            (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue"
             f"\r\n--{boundary}--\r\n").encode()
        req = urllib.request.Request(self.url + "/upload/image", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ComfyUI rejected the upload: {e.read().decode(errors='replace')[:500]}")
        return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]

    def fetch(self, image: dict) -> bytes:
        q = urllib.parse.urlencode({k: image.get(k, "") for k in ("filename", "subfolder", "type")})
        return self._get(f"/view?{q}", timeout=120)


# ---------------------------------------------------------------- post-processing

def remove_bg(path: Path) -> Path:
    from rembg import remove  # lazy: heavy import, only icons need it
    out = path.with_name(path.stem + "-transparent.png")
    out.write_bytes(remove(path.read_bytes()))
    return out


POST = {"remove_bg": remove_bg}


# ---------------------------------------------------------------- source images (img2img / inpaint)

SOURCE_PIXELS = 1024 * 1024  # SDXL's native area; sources are resized to about this
MASK_FEATHER = 4  # px of blur on mask edges


def _source_size(w: int, h: int) -> tuple[int, int]:
    """Keep the aspect ratio, scale to ~1 MP, round each side to a multiple of 64."""
    k = (SOURCE_PIXELS / (w * h)) ** 0.5
    return max(64, round(w * k / 64) * 64), max(64, round(h * k / 64) * 64)


def prepare_source(path: Path, mask: Path | None = None) -> tuple[bytes, bytes | None, tuple[int, int]]:
    """Load the source (and optional mask) as PNG bytes at an SDXL-friendly size.

    Transparency is flattened onto white, so icons and cut-outs work as sources.
    Mask: white = repaint, black = keep. It is resized to the source's size and its edge is
    feathered by MASK_FEATHER px, so pixels just outside the white area blend a little.
    Output is at this ~1 MP size, not the source's resolution.
    """
    from PIL import Image  # lazy: only img2img needs it

    def png(im) -> bytes:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    from PIL import ImageOps
    im = ImageOps.exif_transpose(Image.open(path))  # phone photos: apply the EXIF rotation
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    size = _source_size(*im.size)
    src = png(im.convert("RGB").resize(size, Image.LANCZOS))
    m = None
    if mask is not None:
        mk = ImageOps.exif_transpose(Image.open(mask))
        from PIL import ImageFilter
        # feather the edge a little so the repainted part blends into the kept part
        m = png(mk.convert("L").resize(size, Image.LANCZOS)
                .filter(ImageFilter.GaussianBlur(MASK_FEATHER)).convert("RGB"))
    return src, m, size


def find_sidecar(path: Path) -> dict | None:
    """The JSON sidecar written next to a generated image (also for -transparent.png files)."""
    for cand in (path.with_suffix(".json"),
                 path.with_name(path.stem.removesuffix("-transparent") + ".json")):
        if cand.is_file():
            try:
                meta = json.loads(cand.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(meta, dict):
                return meta
    return None


# ---------------------------------------------------------------- helpers

def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "image"


def to_windows_path(p: Path | str) -> str | None:
    try:
        return subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def to_linux_path(p: str) -> Path:
    """Accept C:\\... or \\\\wsl$\\... (Windows) or a normal Linux path."""
    if re.match(r"^[A-Za-z]:[\\/]", p) or p.startswith("\\\\"):
        out = subprocess.run(["wslpath", "-u", p], capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    return Path(p).expanduser()


# ---------------------------------------------------------------- main API

def generate(prompt: str, preset: str = "draft", count: int = 1, seed: int | None = None,
             negative: str | None = None, width: int | None = None, height: int | None = None,
             steps: int | None = None, cfg: float | None = None, label: str | None = None,
             source_image: str | None = None, mask: str | None = None,
             denoise: float | None = None) -> dict:
    """txt2img by default. With source_image: img2img (denoise = how much may change, 0-1).
    With source_image + mask: inpaint, only the white part of the mask is repainted."""
    conf = load_config()
    presets = conf["presets"]
    if preset not in presets:
        raise ValueError(f"unknown preset '{preset}'. Available: {', '.join(presets)}")
    p = dict(presets[preset])
    count = max(1, int(count))
    if mask is not None and source_image is None:
        raise ValueError("a mask needs a source image")

    workflow = p["workflow"]
    src_path = mask_path = None
    if source_image is not None:
        src_path = to_linux_path(source_image)
        if not src_path.is_file():
            raise FileNotFoundError(src_path)
        if mask is not None:
            mask_path = to_linux_path(mask)
            if not mask_path.is_file():
                raise FileNotFoundError(mask_path)
            workflow = p.get("inpaint_workflow", "sdxl_inpaint.json")
        else:
            workflow = p.get("img2img_workflow", "sdxl_img2img.json")
        denoise = float(denoise if denoise is not None else (0.85 if mask_path else 0.4))
        if not 0 < denoise <= 1:
            raise ValueError("strength/denoise must be in (0, 1]")
        src_png, mask_png, src_size = prepare_source(src_path, mask_path)
    template = json.loads((ROOT / "workflows" / workflow).read_text())
    base_seed = seed if seed is not None else random.randrange(MAX_SEED)
    full_prompt = prompt + p.get("suffix", "")
    full_negative = (negative if negative is not None else conf.get("default_negative", "")) \
        + p.get("negative_extra", "")

    params = {k: v for k, v in p.items() if k not in ("workflow", "img2img_workflow", "inpaint_workflow",
                                                      "description", "suffix", "negative_extra", "post")}
    params.update(checkpoint=p.get("checkpoint", conf["checkpoint"]),
                  prompt=full_prompt, negative=full_negative)
    for k, v in (("width", width), ("height", height), ("steps", steps), ("cfg", cfg)):
        if v is not None:
            params[k] = v

    now = datetime.now()
    run_dir = Path(conf["output_dir"]).expanduser() / now.strftime("%Y-%m-%d") / \
        f"{now:%H%M%S}-{preset}-{slug(label or prompt)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    comfy = Comfy(conf["comfy_url"])
    comfy.wait_ready()

    source = None
    if src_path is not None:
        # content-addressed names: re-running on the same source/mask overwrites instead of piling up
        def name(kind: str, data: bytes) -> str:
            return f"imagegen-{kind}-{hashlib.sha1(data).hexdigest()[:16]}.png"
        params.update(image=comfy.upload(src_png, name("src", src_png)), denoise=denoise,
                      width=src_size[0], height=src_size[1])
        if mask_png is not None:
            params["mask"] = comfy.upload(mask_png, name("mask", mask_png))
        source = {"source_image": str(src_path), "mask": str(mask_path) if mask_path else None,
                  "denoise": denoise}

    # queue everything up front; ComfyUI works through it while we wait
    jobs = []
    for i in range(count):
        s = (base_seed + i) % MAX_SEED
        jobs.append((s, comfy.queue(fill(template, {**params, "seed": s}))))
    log(f"queued {count} x {preset} (seeds {base_seed}..) -> {run_dir}")

    images = []
    for i, (s, pid) in enumerate(jobs, 1):
        t0 = time.time()
        outputs = comfy.wait(pid)
        outs = [im for o in outputs.values() for im in o.get("images", []) if im.get("type") == "output"]
        for j, im in enumerate(outs):
            name = f"{i:02d}-seed{s}" + (f"-{j}" if len(outs) > 1 else "") + ".png"
            path = run_dir / name
            path.write_bytes(comfy.fetch(im))
            final = path
            for step in p.get("post", []):
                final = POST[step](final)
            meta = {"prompt": prompt, "full_prompt": full_prompt, "negative": full_negative,
                    "user_negative": negative,
                    "preset": preset, "seed": s, "params": {k: v for k, v in params.items()
                                                            if k not in ("prompt", "negative")},
                    "file": final.name, "raw_file": path.name, "created": now.isoformat(timespec="seconds"),
                    **(source or {})}
            path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
            images.append({"path": str(final), "windows_path": to_windows_path(final), "seed": s})
        log(f"  [{i}/{count}] seed {s} done in {time.time() - t0:.1f}s")

    run = {"preset": preset, "prompt": prompt, "base_seed": base_seed, "count": count,
           "run_dir": str(run_dir), "images": images, "transparent": bool(p.get("post")), **(source or {})}
    (run_dir / "run.json").write_text(json.dumps(run, indent=2))
    sheet = write_contact_sheet(run_dir, run)
    run["contact_sheet"] = str(sheet)
    run["contact_sheet_windows"] = to_windows_path(sheet)
    run["run_dir_windows"] = to_windows_path(run_dir)
    return run


def vary(path: str, count: int = 4, strength: float | None = None, prompt: str | None = None,
         negative: str | None = None, preset: str | None = None, mask: str | None = None,
         seed: int | None = None, steps: int | None = None, cfg: float | None = None,
         label: str | None = None) -> dict:
    """Variations of an existing image (img2img), or inpaint with a mask.

    Prompt, preset and negative default to the image's JSON sidecar when it has one;
    an outside image (no sidecar) needs a prompt.
    """
    src = to_linux_path(path)
    if not src.is_file():
        raise FileNotFoundError(src)
    meta = find_sidecar(src) or {}
    if prompt is None:
        prompt = meta.get("prompt")
        if prompt is None:
            raise ValueError(f"no sidecar JSON next to {src.name}; pass a prompt")
    if negative is None:
        negative = meta.get("user_negative")
    preset = preset or meta.get("preset") or "draft"
    return generate(prompt, preset, count, seed, negative, steps=steps, cfg=cfg,
                    label=label or ("inpaint-" if mask else "vary-") + slug(src.stem, 30),
                    source_image=str(src), mask=mask, denoise=strength)


SHEET_TEMPLATE = ROOT / "templates" / "contact_sheet.html"
SHEET_PARAMS = ("width", "height", "steps", "cfg")  # shown per image when the sidecar has them


def write_contact_sheet(run_dir: Path, run: dict) -> Path:
    """Write <run_dir>/index.html: the viewer in templates/contact_sheet.html with this run's data inlined.

    Checkerboard background when run["transparent"]; older run.json files lack it, so then
    it is guessed from the file names (rembg output ends in -transparent.png).
    """
    images, first = [], None
    for im in run["images"]:
        path = Path(im["path"])
        meta = find_sidecar(run_dir / path.name) or {}
        first = first if first is not None else meta
        entry = {"file": path.name, "seed": im["seed"]}
        params = meta.get("params") or {}
        entry.update({k: params[k] for k in SHEET_PARAMS if k in params})
        images.append(entry)
    transparent = run.get("transparent")
    if transparent is None:
        transparent = any(e["file"].endswith("-transparent.png") for e in images)
    data = {"prompt": run["prompt"], "preset": run["preset"], "base_seed": run["base_seed"],
            "count": run["count"], "transparent": transparent, "images": images,
            "negative": (first or {}).get("negative"),
            "source_image": Path(run["source_image"]).name if run.get("source_image") else None,
            "mask": bool(run.get("mask")), "denoise": run.get("denoise")}
    # "<" escaped so no value can close the <script> block it is inlined into
    fills = {"TITLE": html.escape(run["prompt"][:60]),
             "RUN_JSON": json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")}
    page = re.sub(r"\{\{(TITLE|RUN_JSON)\}\}", lambda m: fills[m.group(1)], SHEET_TEMPLATE.read_text(encoding="utf-8"))
    out = run_dir / "index.html"
    out.write_text(page, encoding="utf-8")
    return out


def find_run_files(path: str | None = None) -> list[Path]:
    """The run.json files a user means by `path`.

    None = the latest run. Otherwise a run folder, its run.json or index.html, or a date folder
    (all its runs); relative paths are also tried under output_dir, so `2026-10-31` works anywhere.
    """
    root = Path(load_config()["output_dir"]).expanduser()
    if path is None:
        runs = sorted(root.glob("*/*/run.json"), key=lambda f: f.stat().st_mtime)
        return runs[-1:]
    if re.match(r"^[A-Za-z]:[^\\/]", path):  # bash ate the backslashes of an unquoted C:\... path
        raise FileNotFoundError(f"{path} (put Windows paths in quotes: gen sheet 'C:\\...')")
    p = to_linux_path(path)
    # relative: as given, under output_dir, and as absolute (a dropped leading slash: mnt/c/...)
    for c in (p, root / p, Path("/") / p) if not p.is_absolute() else (p,):
        if c.name == "run.json" and c.is_file():
            return [c]
        if c.suffix and (c.parent / "run.json").is_file():  # index.html or an image
            return [c.parent / "run.json"]
        if (c / "run.json").is_file():
            return [c / "run.json"]
        if c.is_dir() and (runs := sorted(c.glob("*/run.json"))):  # a date folder
            return runs
    raise FileNotFoundError(f"no run.json in or under {p}")


def rebuild_contact_sheets(path: str | None = None) -> list[Path]:
    """Re-write index.html from run.json for every run find_run_files(path) finds; returns the sheets."""
    files = find_run_files(path)
    if not files:
        raise FileNotFoundError("no runs yet")
    return [write_contact_sheet(f.parent, json.loads(f.read_text())) for f in files]


def recent_runs(n: int = 5) -> list[dict]:
    root = Path(load_config()["output_dir"]).expanduser()
    runs = sorted(root.glob("*/*/run.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:n]
    out = []
    for f in runs:
        r = json.loads(f.read_text())
        out.append({"run_dir": r["run_dir"], "preset": r["preset"], "prompt": r["prompt"],
                    "images": [im["path"] for im in r["images"]]})
    return out


def set_wallpaper(path: str) -> str:
    p = to_linux_path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    win = to_windows_path(p)
    if not win:
        raise RuntimeError("wslpath failed (are we running inside WSL?)")
    if win.startswith("\\\\"):
        # Windows can't reliably use \\wsl$ paths as wallpaper
        raise RuntimeError("wallpaper must live on a Windows drive (set output_dir under /mnt/c)")
    ps = ("Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class W { "
          "[DllImport(\"user32.dll\", CharSet=CharSet.Unicode)] public static extern int "
          "SystemParametersInfo(int a, int b, string c, int d); }'; "
          f"[void][W]::SystemParametersInfo(20, 0, '{win.replace(chr(39), chr(39) * 2)}', 3)")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                   check=True, capture_output=True)
    return win


def open_in_windows(path: str) -> None:
    win = to_windows_path(path)
    if win and shutil.which("explorer.exe"):
        subprocess.run(["explorer.exe", win])  # explorer returns 1 even on success


# ---------------------------------------------------------------- CLI

def _print_run(run: dict, open_sheet: bool) -> None:
    for im in run["images"]:
        print(im["windows_path"] or im["path"])
    print("contact sheet:", run["contact_sheet_windows"] or run["contact_sheet"])
    if open_sheet:
        open_in_windows(run["contact_sheet"])


def main(argv: list[str]) -> int:
    import argparse
    if argv and argv[0] == "presets":
        for name, p in list_presets().items():
            print(f"{name:22} {p.get('description', '')}")
        return 0
    if argv and argv[0] == "recent":
        for r in recent_runs(int(argv[1]) if len(argv) > 1 else 5):
            print(f"{r['preset']:12} {r['prompt'][:50]:50} {r['run_dir']}")
        return 0
    if argv and argv[0] == "doctor":
        import doctor
        return doctor.main()
    if argv and argv[0] == "vary":
        vp = argparse.ArgumentParser(prog="gen vary",
                                     description="img2img variations of an image, or inpaint with --mask")
        vp.add_argument("path")
        vp.add_argument("-n", "--count", type=int, default=4)
        vp.add_argument("-s", "--strength", type=float,
                        help="how much may change, 0-1 (default 0.4; 0.85 with --mask)")
        vp.add_argument("--prompt", help="default: the image's own prompt from its sidecar")
        vp.add_argument("--neg", dest="negative")
        vp.add_argument("-p", "--preset", help="default: the image's own preset, else draft")
        vp.add_argument("--mask", help="PNG, white = repaint, black = keep (any size)")
        vp.add_argument("--seed", type=int)
        vp.add_argument("--steps", type=int)
        vp.add_argument("--cfg", type=float)
        vp.add_argument("--label", help="folder name instead of vary-<image>")
        vp.add_argument("--open", action="store_true", help="open contact sheet in Windows browser")
        a = vp.parse_args(argv[1:])
        run = vary(a.path, a.count, a.strength, a.prompt, a.negative, a.preset, a.mask,
                   a.seed, a.steps, a.cfg, a.label)
        _print_run(run, a.open)
        return 0
    if argv and argv[0] == "cutout":
        if len(argv) != 2:
            print("usage: gen cutout PATH", file=sys.stderr)
            return 2
        src = to_linux_path(argv[1])
        if not src.is_file():
            print(f"not a file: {src}", file=sys.stderr)
            return 1
        print(remove_bg(src))
        return 0
    if argv and argv[0] == "sheet":
        if argv[1:2] in (["-h"], ["--help"]):
            print("usage: gen sheet [PATH...]  rebuild index.html from run.json\n"
                  "  PATH: a run folder (or its index.html), or a date folder for all its runs;\n"
                  "  relative paths are looked up under output_dir. No PATH = the latest run.")
            return 0
        status = 0
        for d in argv[1:] or [None]:
            try:
                for out in rebuild_contact_sheets(d):
                    print(to_windows_path(out) or out)
            except (FileNotFoundError, KeyError, ValueError) as e:  # ValueError covers bad JSON
                print(f"{d or 'latest run'}: cannot rebuild ({type(e).__name__}: {e})", file=sys.stderr)
                status = 1
        return status
    if argv and argv[0] == "set-wallpaper":
        print("wallpaper set:", set_wallpaper(argv[1]))
        return 0

    ap = argparse.ArgumentParser(prog="gen", description="Generate images via ComfyUI presets")
    ap.add_argument("prompt")
    ap.add_argument("-p", "--preset", default="draft")
    ap.add_argument("-n", "--count", type=int, default=1)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--neg", dest="negative")
    ap.add_argument("--size", help="WxH generation size, e.g. 832x1216")
    ap.add_argument("--steps", type=int)
    ap.add_argument("--cfg", type=float)
    ap.add_argument("--label", help="folder name instead of prompt slug")
    ap.add_argument("--open", action="store_true", help="open contact sheet in Windows browser")
    ap.add_argument("--wallpaper", action="store_true", help="set the first result as wallpaper")
    a = ap.parse_args(argv)
    w = h = None
    if a.size:
        w, h = (int(x) for x in a.size.lower().split("x"))
    run = generate(a.prompt, a.preset, a.count, a.seed, a.negative, w, h, a.steps, a.cfg, a.label)
    _print_run(run, a.open)
    if a.wallpaper and run["images"]:
        print("wallpaper set:", set_wallpaper(run["images"][0]["path"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (RuntimeError, ValueError, KeyError, FileNotFoundError, TimeoutError) as e:
        log(f"error: {e}")
        sys.exit(1)
